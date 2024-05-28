import Analyze
import Mutation
from Protein import Protein
import pandas as pd
import os
import argparse
from multiprocessing import cpu_count
from multiprocessing.pool import ThreadPool as Pool
import tqdm
from functools import partial
from utils import print_if, adaptive_chunksize, afm_iterator
from definitions import *
import glob
import time
from math import ceil



def all_proteins():
    """
    :return: iterable of all Protein objects in DB
    """
    for path in glob.glob(os.path.join(PROTEIN_PATH, '*')):
        prot_name = os.path.basename(path)
        yield Protein(ref_name=prot_name)


def all_mutations():
    for prot in all_proteins():
        for mutation in prot.generate_mutations():
            yield mutation


def create_parser():
    parser = argparse.ArgumentParser(
        description="Pipeline interface for protein-level analysis of missense variants in familial data"
    )

    parser.add_argument(
        "--action",
        type=str,
        choices=["init-DB", "update-DB", "delete-DB", "score-ESM", "score-EVE", "score-AFM"],
        default="init-DB",
        help="init-DB: initialize project database according to the supplied in the csv file\n"
             "update-DB: update project database according to the supplied in the csv file\n"
             "score-[model]: calculate [model] scores for all missense variants in DB",
        nargs="+",
    )

    parser.add_argument(
        "--data-path",
        type=str,
        default='data.csv',
        help="path to csv file containing the data see README for expected format",
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=0,
        help="number of CPUs to run on, if not given will use maximum CPUs available",
    )

    parser.add_argument(
        "--protein-col",
        type=str,
        default="Protein",
        help="column name holding the protein reference name in csv file i.e. PT53",
    )

    parser.add_argument(
        "--variant-col",
        type=str,
        default="Variant",
        help="column name holding the missense variant description in csv file i.e. D17Y | p.D17Y",
    )

    parser.add_argument(
        "--patient-col",
        type=str,
        default="Patient",
        help="column name holding the patient i.d",
    )

    parser.add_argument(
        "--family-col",
        type=str,
        default="Family",
        help="column name holding the family i.d",
    )

    parser.add_argument(
        "--chromosome-col",
        type=str,
        default="Chr",
        help="column name holding the chromosome in which the gene is found",
    )

    parser.add_argument(
        "--dna-start-col",
        type=str,
        default="Start",
        help="column name holding the variant dna start index",
    )

    parser.add_argument(
        "--dna-end-col",
        type=str,
        default="End",
        help="column name holding the variant dna end index",
    )

    parser.add_argument(
        "--wt-col",
        type=str,
        default="Ref",
        help="column name holding the variant dna wt (reference) nucleic acid",
    )

    parser.add_argument(
        "--alt-col",
        type=str,
        default="Alt",
        help="column name holding the variant dna altered nucleic acid",
    )

    parser.add_argument(
        "-v", "--verbose",
        type=int,
        choices=[0, 1, 2, 3],
        default=1,
        help="level of description (2-3 not recommended with workers > 1) : \n"
             "0 - Only major errors will be printed no progress information will be printed\n"
             "1 - default, program progress information and warnings will be printed\n"
             "2 - high level process and warnings i.e. protein being created\n"
             "3 - low level process information and raw errors messages i.e. detailed steps of protein creation, "
             "mutation creation etc...\n "
    )

    return parser


def create_new_records(args, *rowiters):
    """
    creates protein and mutation object from csv row
    :param args: user arguments
    :param rowiter: DataFrame rows iterator
    :return: bool success
    """
    idxs = [t[0] for t in rowiters]
    rows = [t[1] for t in rowiters]
    rowiter = zip(idxs, rows)
    skipped = []
    created_protein = None
    for idx, row in rowiter:
        gene = row[args.protein_col]
        mut_desc = row[args.variant_col]
        dna = {'chr': row[args.chromosome_col], 'start': row[args.dna_start_col], 'end': row[args.dna_end_col],
               'ref_na': row[args.wt_col], 'alt_na': row[args.alt_col]}
        try:
            protein = Protein(ref_name=gene, verbose_level=args.verbose) if not created_protein else created_protein
            created_protein = protein
            protein.add_mut(mut_desc, dna)
        except TimeoutError:
            print_if(args.verbose, VERBOSE['thread_warnings'], f"skipped {gene} due to timeout")
            os.remove(rf'DB\test_p\{gene}')
            os.remove(rf'DB\test_m\{gene}_{mut_desc}.txt')
            skipped.append(idx)
    return skipped


def build_db(args, target, workers):
    skipped = []
    df = pd.read_csv(args.data_path)
    values_iter = lambda value, data: data[data[args.protein_col] == value].iterrows()

    # tasks are per-protein iterators to prevent race conditions
    # in proteins with multiple mutations
    unique_rows = df[~df[args.protein_col].duplicated(keep=False)]
    unique_proteins = unique_rows[args.protein_col].unique()
    tasks_unique = [values_iter(value, unique_rows) for value in unique_proteins]

    repeating_rows = df[df[args.protein_col].duplicated(keep=False)]
    repeating_proteins = repeating_rows[args.protein_col].unique()
    tasks_repeating = [values_iter(value, repeating_rows) for value in repeating_proteins]

    tasks = tasks_repeating + tasks_unique
    print_if(args.verbose, VERBOSE['program_progress'], f"Building protein database...")
    with Pool(workers) as p:
        for status in p.starmap(target, tasks):
            if status:  # if failed will return row index
                skipped += status

    if args.verbose >= VERBOSE['program_progress']:
        if not skipped:
            print_if(args.verbose, VERBOSE['program_progress'], f"done, successfully created all records")
        else:
            print_if(args.verbose, VERBOSE['program_progress'], f"done, skipped records in indexes:\n"
                                                                f"{', '.join(str(x) for x in skipped)}")

    return skipped


#def calc_mutations_afm_scores(args, analyzer, chunk=None, *mutations):
def calc_mutations_afm_scores(args, analyzer, chunk=None, iter_desc='', use_alias=False, recalc=False):
    """
    calculates Alpha Missense scores for all the mutations of a specific protein
    :param recalc: bool re-calculate scored to mutations with scores
    :param use_alias: bool should reviewed uid aliases be searched
    :param args:
    :param analyzer: ProteinAnalyzer object
    :param chunk: optional df chunk to search mutation in
    :param mutations: Iterator of Mutations
    :return:
    """
    successful = 0
    if recalc:
        tasks = list(all_mutations())
    else:
        tasks = [mut for mut in all_mutations() if not mut.has_afm]
    n_muts = len(tasks)
    if chunk is not None:
        uid_index = set(chunk['uniprot_id'].unique())
    for mutation in tqdm.tqdm(tasks, desc=iter_desc, total=n_muts):
        print_if(args.verbose, VERBOSE['thread_progress'], f"Calculating AlphaMissense scores for {mutation.long_name}")
        score = analyzer.score_mutation_afm(mutation, chunk=chunk, uid_index=uid_index, use_alias=use_alias)
        successful += score is not None
        mutation.update_score('AFM', score)
    return successful


def main(args):
    workers = args.workers if args.workers else cpu_count()
    print_if(args.verbose, VERBOSE['program_progress'], f"running on {workers} CPUs")
    for action in args.action:
        if action == 'init-DB':
            assert os.path.exists(args.data_path), f"could not locate data csv file at {args.data_path}"
            target = partial(create_new_records, args)
            skipped = build_db(args, target=target, workers=workers)
            return skipped
        if action == 'score-AFM':
            analyzer = Analyze.ProteinAnalyzer()
            chunksize = adaptive_chunksize(AFM_ROWSIZE, 0.25)
            n_muts, iter_num, total_iter, total_scores =len(glob.glob(pjoin(MUTATION_PATH, '*.txt'))), 1, \
                                                        ceil(AFM_ROWS / chunksize), 0
            print_if(args.verbose, VERBOSE['program_progress'], f"Calculating AlphaMissense scores...")
            print(chunksize)
            for chunk in afm_iterator(int(chunksize), usecols=['uniprot_id', 'protein_variant', 'am_pathogenicity']):
                total_scores += calc_mutations_afm_scores(args, analyzer, chunk, f'iter {iter_num} of {total_iter} ',
                                                     use_alias=False, recalc=False)
                iter_num += 1
            # Second run with aliases for mutations without scores
            print_if(args.verbose, VERBOSE['program_progress'], f"Expending search for unresolved mutations...")
            iter_num = 1
            for chunk in afm_iterator(int(chunksize), usecols=['uniprot_id', 'protein_variant', 'am_pathogenicity']):
                total_scores += calc_mutations_afm_scores(args, analyzer, chunk, f'iter {iter_num} of {total_iter}',
                                                     use_alias=True, recalc=False)
                iter_num += 1
            print_if(args.verbose, VERBOSE['program_progress'], f"done, scored {total_scores} of {n_muts} mutations")


if __name__ == "__main__":
    parser = create_parser()
    args = parser.parse_args()
    start_time = time.time()
    main(args)
    print("--- %s seconds ---" % (time.time() - start_time))

