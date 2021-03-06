import os
import sys
import Bio.SeqIO
from iggtools.common.argparser import add_subcommand, SUPPRESS
from iggtools.common.utils import tsprint, InputStream, retry, command, multithreading_map, find_files, upload, pythonpath, upload_star, num_physical_cores, download_reference
from iggtools.models.uhgg import UHGG
from iggtools.params import inputs, outputs


CONCURRENT_MARKER_GENES_IDENTIFY = num_physical_cores


def input_annotations_file(genome_id, species_id, filename):
    # s3://microbiome-igg/2.0/gene_annotations/{SPECIES_ID}/{GENOME_ID}/{GENOME_ID}.{fna, faa, gff, log}
    return f"{outputs.annotations}/{species_id}/{genome_id}/{filename}"


def output_marker_genes_file(genome_id, species_id, filename):
    # s3://{igg}/marker_genes/phyeco/temp/{SPECIES_ID}/{GENOME_ID}/{GENOME_ID}.{hmmsearch, markers.fa, markers.map}
    return f"{outputs.marker_genes}/temp/{species_id}/{genome_id}/{filename}"


def lastoutput(genome_id):
    return f"{genome_id}.markers.map"


def destpath(genome_id, species_id, src):
    return output_marker_genes_file(genome_id, species_id, src + ".lz4")


def hmmsearch(genome_id, species_id, marker_genes_hmm, num_threads=1):
    # Input
    annotated_genes_s3_path = input_annotations_file(genome_id, species_id, f"{genome_id}.faa.lz4")
    annotated_genes = download_reference(annotated_genes_s3_path)

    # Output
    hmmsearch_file = f"{genome_id}.hmmsearch"

    # Command
    if find_files(hmmsearch_file):
        # This only happens in debug mode, where we can use pre-existing file.
        tsprint(f"Found hmmsearch results for genome {genome_id} from prior run.")
    else:
        try:
            command(f"hmmsearch --noali --cpu {num_threads} --domtblout {hmmsearch_file} {marker_genes_hmm} {annotated_genes}")
        except:
            # Do not keep bogus zero-length files;  those are harmful if we rerun in place.
            command(f"mv {hmmsearch_file} {hmmsearch_file}.bogus", check=False)
            raise

    return hmmsearch_file


@retry
def fetch_genes(annotated_genes):
    """" Lookup of seq_id to sequence for PATRIC genes """
    gene_seqs = {}
    with InputStream(annotated_genes) as genes:
        for rec in Bio.SeqIO.parse(genes, 'fasta'):
            gene_seqs[rec.id] = str(rec.seq).upper()
    return gene_seqs


def parse_hmmsearch(hmmsearch_file):
    """ Parse HMMER domblout files. Return data-type formatted dictionary """
    with InputStream(hmmsearch_file) as f_in:
        for line in f_in:
            if line[0] == "#":
                continue
            x = line.rstrip().split()
            query = x[0]
            target = x[3]
            evalue = float(x[12])
            qcov = (int(x[20]) - int(x[19]) + 1)/float(x[2])
            tcov = (int(x[16]) - int(x[15]) + 1)/float(x[5])
            yield {'query':query, 'target':target, 'evalue':evalue, 'qcov':qcov, 'tcov':tcov, 'qlen':int(x[2]), 'tlen':int(x[5])}


def find_hits(hmmsearch_file):
    # Input
    max_evalue = inputs.hmmsearch_max_evalue
    min_cov = inputs.hmmsearch_min_cov

    hits = {}
    for r in parse_hmmsearch(hmmsearch_file):
        if r['evalue'] > max_evalue:
            continue
        if min(r['qcov'], r['tcov']) < min_cov:
            continue
        if r['target'] not in hits:
            hits[r['target']] = r
        elif r['evalue'] < hits[r['target']]['evalue']:
            hits[r['target']] = r
    return list(hits.values())


def identify_marker_genes(genome_id, species_id, marker_genes_hmm):

    command(f"aws s3 rm --recursive {output_marker_genes_file(genome_id, species_id, '')}")

    hmmsearch_file = hmmsearch(genome_id, species_id, marker_genes_hmm, num_threads=1)

    annotated_genes_s3_path = input_annotations_file(genome_id, species_id, f"{genome_id}.ffn.lz4")
    genes = fetch_genes(annotated_genes_s3_path)

    # Parse local hmmsearch file
    hmmsearch_seq = f"{genome_id}.markers.fa"
    hmmsearch_map = f"{genome_id}.markers.map"

    with open(hmmsearch_seq, "w") as o_seq, open(hmmsearch_map, "w") as o_map:
        for rec in find_hits(hmmsearch_file):
            marker_gene = genes[rec["query"]].upper()
            marker_info = [species_id, genome_id, rec["query"], len(marker_gene), rec["target"]]
            o_map.write('\t'.join(str(mi) for mi in marker_info) + '\n')
            o_seq.write('>%s\n%s\n' % (rec['query'], marker_gene))

    output_files = [hmmsearch_file, hmmsearch_seq, hmmsearch_map]
    # Make sure output hmmsearch_map last cuz it indicates all other files has successed
    assert output_files[-1] == lastoutput(genome_id)

    return output_files

def build_marker_genes(args):
    if args.zzz_slave_toc:
        build_marker_genes_slave(args)
    else:
        build_marker_genes_master(args)


@retry
def find_files_with_retry(f):
    return find_files(f)


def decode_genomes_arg(args, genomes):
    selected_genomes = set()
    try:  # pylint: disable=too-many-nested-blocks
        if args.genomes.upper() == "ALL":
            selected_genomes = set(genomes)
        else:
            for g in args.genomes.split(","):
                if ":" not in g:
                    selected_genomes.add(g)
                else:
                    i, n = g.split(":")
                    i = int(i)
                    n = int(n)
                    assert 0 <= i < n, f"Genome class and modulus make no sense: {i}, {n}"
                    for gid in genomes:
                        gid_int = int(gid.replace("GUT_GENOME", ""))
                        if gid_int % n == i:
                            selected_genomes.add(gid)
    except:
        tsprint(f"ERROR:  Genomes argument is not a list of genome ids or slices: {g}")
        raise
    return sorted(selected_genomes)


def build_marker_genes_master(args):

    # Fetch table of contents and marker genes HMM model from s3.
    # This will be read separately by each species build subcommand, so we make a local copy.
    local_toc, marker_genes_hmm = multithreading_map(download_reference, [outputs.genomes, inputs.marker_genes_hmm])

    db = UHGG(local_toc)
    species_for_genome = db.genomes

    def genome_work(genome_id):
        assert genome_id in species_for_genome, f"Genome {genome_id} is not in the database."
        species_id = species_for_genome[genome_id]

        dest_file = destpath(genome_id, species_id, lastoutput(genome_id))
        msg = f"Running HMMsearch for genome {genome_id} from species {species_id}."
        if find_files_with_retry(dest_file):
            if not args.force:
                tsprint(f"Destination {dest_file} for genome {genome_id} already exists.  Specify --force to overwrite.")
                return
            msg = msg.replace("Running", "Rerunning")

        tsprint(msg)
        slave_log = "build_marker_genes.log"
        slave_subdir = f"{species_id}__{genome_id}"
        if not args.debug:
            command(f"rm -rf {slave_subdir}")
        if not os.path.isdir(slave_subdir):
            command(f"mkdir {slave_subdir}")

        # Recurisve call via subcommand.  Use subdir, redirect logs.
        slave_cmd = f"cd {slave_subdir}; PYTHONPATH={pythonpath()} {sys.executable} -m iggtools build_marker_genes --genome {genome_id} --zzz_slave_mode --zzz_slave_toc {os.path.abspath(local_toc)} --zzz_slave_marker_genes_hmm {os.path.abspath(marker_genes_hmm)} {'--debug' if args.debug else ''} &>> {slave_log}"
        with open(f"{slave_subdir}/{slave_log}", "w") as slog:
            slog.write(msg + "\n")
            slog.write(slave_cmd + "\n")
        try:
            command(slave_cmd)
        finally:
            # Cleanup should not raise exceptions of its own, so as not to interfere with any
            # prior exceptions that may be more informative.  Hence check=False.
            upload(f"{slave_subdir}/{slave_log}", destpath(genome_id, species_id, slave_log), check=False)
            if not args.debug:
                command(f"rm -rf {slave_subdir}", check=False)

    genome_id_list = decode_genomes_arg(args, species_for_genome)
    multithreading_map(genome_work, genome_id_list, num_threads=CONCURRENT_MARKER_GENES_IDENTIFY)


def build_marker_genes_slave(args):
    """
    https://github.com/czbiohub/iggtools/wiki
    """

    violation = "Please do not call build_merker_genes_slave directly.  Violation"
    assert args.zzz_slave_mode, f"{violation}:  Missing --zzz_slave_mode arg."
    assert os.path.isfile(args.zzz_slave_toc), f"{violation}: File does not exist: {args.zzz_slave_toc}"
    assert os.path.isfile(args.zzz_slave_marker_genes_hmm), f"{violation}: Maker genes HMM model file does not exist: {args.zzz_slave_marker_genes_hmm}"

    db = UHGG(args.zzz_slave_toc)
    species_for_genome = db.genomes

    genome_id = args.genomes
    species_id = species_for_genome[genome_id]
    marker_genes_hmm = args.zzz_slave_marker_genes_hmm

    output_files = identify_marker_genes(genome_id, species_id, marker_genes_hmm)

    # Upload to S3
    upload_tasks = []
    for o in output_files[:-1]:
        upload_tasks.append((o, destpath(genome_id, species_id, o)))
    multithreading_map(upload_star, upload_tasks)

    # Upload this last because it indicates all other work has succeeded.
    upload(output_files[-1], destpath(genome_id, species_id, output_files[-1]))


def register_args(main_func):
    subparser = add_subcommand('build_marker_genes', main_func, help='identify marker genes for  given genomes')
    subparser.add_argument('--genomes',
                           dest='genomes',
                           required=False,
                           help="genome[,genome...] to import;  alternatively, slice in format idx:modulus, e.g. 1:30, meaning import genomes whose ids are 1 mod 30; or, the special keyword 'all' meaning all genomes")
    subparser.add_argument('--zzz_slave_toc',
                           dest='zzz_slave_toc',
                           required=False,
                           help=SUPPRESS) # "reserved to pass table of contents from master to slave"
    subparser.add_argument('--zzz_slave_marker_genes_hmm',
                           dest='zzz_slave_marker_genes_hmm',
                           required=False,
                           help=SUPPRESS) # "reserved to common database from master to slave"
    return main_func


@register_args
def main(args):
    tsprint(f"Executing iggtools subcommand {args.subcommand} with args {vars(args)}.")
    build_marker_genes(args)
