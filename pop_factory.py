"""
Generates fake data for similating possible scenarios for use in PLINK.

1. Read config/command
2. Read SNP data
3. Generate PED/MAP files based on command and SNP data

Similar to http://cnsgenomics.com/software/gcta/#GWASSimulation ?
"""
import gc
import getopt
import json
import random
import sys
import glob
from multiprocessing import Process, Queue, Condition
import io
from common.snp import RefSNP, Allele, is_haploid, chromosome_from_filename, split_list
from common.synchro import SynchCondition
from download import OUTPUT_DIR
import re
import numpy
import os
from datetime import datetime
import gzip
from yaml import load
from common.db import db
from common.timer import Timer

try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader

MIN_SNP_FREQ = 0.005
MIN_TOTAL_COUNT = 1000
OUTPUT_DIR = "populations"
SNP_DIR = "output"
CHROMOSOME_LIST = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12', '13', '14', '15',
                   '16', '17', '18', '19', '20', '21', '22', 'X', 'Y']


def gen_vcf_header():
    header = "##fileformat=VCFv4.3\n"
    header += "##filedate=%s\n" % datetime.now().strftime("%Y%m%d %H:%M")
    header += "##source=SNP_Simulator\n"
    header += '##FILTER=<ID=q10,Description="Quality below 10">\n'
    header += '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
    return header


def write_vcf_header(io_stream, fam_data):
    io_stream.write(gen_vcf_header())
    io_stream.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t")
    io_stream.write("\t".join(map(lambda x: str(x.person_id), fam_data)))
    io_stream.write("\n")


class SampleInfo:
    """
    Class for individual sample metadata for use in a .fam file. Also holds pathogen data for this individual
    """

    def __init__(self, family_id, person_id, father_id, mother_id, sex: int, is_control: bool, pathogen_snps: dict):
        assert person_id
        self.person_id = person_id
        self.family_id = family_id
        self.father_id = father_id
        self.mother_id = mother_id
        self.sex = sex
        self.is_control = is_control
        self.pathogen_snps = pathogen_snps

    def to_fam_format(self):
        if self.is_control:
            pheno_code = 1
        else:
            pheno_code = 2
        return "%i\t%i\t%i\t%i\t%i\t%i\t\n" % \
               (self.family_id, self.person_id, self.father_id, self.mother_id, self.sex, pheno_code)

    def is_male(self):
        return self.sex == 1

class SNPTuples:
    """ Class for holding compressed snp and probability data
    """

    def __init__(self, snp_id, chromosome, position):
        self.id = snp_id
        self.chromosome = chromosome
        self.tuples = []
        self.position = position

    def add_tuple(self, inserted, range_end):
        self.tuples.append((inserted, range_end))

    def pick_snp_value(self, random_roll):
        for nt_letter, prob in self.tuples:
            if prob > random_roll:
                return nt_letter

    def pick_allele_index(self, random_roll):
        for i, tupl in enumerate(self.tuples):
            if tupl[1] >= random_roll:
                return i

    def minor_allele_tuple(self):
        """
        Returns the second most probable allele and it's frequency
        :return: second most probable nucleotide for this snp and it's frequency
        """
        return self.tuples[1]

    def ref_allele_tuple(self):
        """
        Returns the most probable allele and it's frequency
        :return: second most probable nucleotide for this snp and it's frequency
        """
        return self.tuples[0]

    def alt_alleles(self):
        if len(self.tuples) == 1:
            return self.tuples[0][0]
        if len(self.tuples) == 2:
            return self.tuples[1][0]
        return ",".join(map(lambda x: x[0], self.tuples[1:]))


class PopulationFactory:

    # number of subgroups with phenotype, total number of hidden mutations
    def __init__(self, num_processes=1):
        self.pathogens = {}
        self.ordered_snps = []
        self.snp_count = 0
        self.population_dir = OUTPUT_DIR
        if num_processes > 0:
            self.num_processes = num_processes
        else:
            self.num_processes = 1

    @Timer(logger=print, text="Finished Generating Population in {:0.4f} secs.")
    def generate_population(self, control_size, test_size, male_odds, pathogens_file, min_freq, max_snps):
        """Generate a simulated population based on the number of groups, mutations, size of test group,
        size of control group and the snp dictionary.
        1. Determine which snps will be the hidden pathogenic snps
        2. Generate control data using random generated population
        3. Generate test data based on hidden pathogens and random otherwise
        Use numpy to generate random number en mass
        """
        subdir = datetime.now().strftime("%Y%m%d%H%M")
        numpy.random.seed(int(datetime.now().strftime("%H%M%S")))
        self.population_dir = OUTPUT_DIR + "/" + subdir + "/"
        os.makedirs(self.population_dir, exist_ok=True)

        self.load_snps_db(min_freq, max_snps)
        gc.collect()
        self.pick_pathogen_snps(self.ordered_snps, pathogens_file)

        # Create control population
        self.output_vcf_population(control_size, test_size, male_odds)
        return

    def load_snps_db(self, min_freq, max_snps):
        """
        Load snps from DB and store as SNPTuples. Also output map file for plink.
        :param max_snps: Max number of snps to load
        :param min_freq: min Minor Allele frequency
        :return:
        """
        with open(self.population_dir + "population.map", 'at') as f:
            invalid_count = 0
            snps_result = db.connection.execute(
                "Select r.id, chromosome, maf, total_count,  deleted, inserted, position, allele_count "
                "from ref_snps r  "
                "join alleles a on r.id = a.ref_snp_id "
                "and r.maf >= %f and r.total_count >= %i" % (min_freq, MIN_TOTAL_COUNT)
            )
            current_snp_id = -1
            snp = None
            for snp_row in snps_result:
                if snp_row["id"] != current_snp_id:
                    if snp and snp.valid_for_plink():
                        if self.snp_count >= max_snps - 1:
                            print("Hit max_snps size of %i. Stopping loading snps." % max_snps)
                            break
                        self.output_map_file_line(f, snp.chromosome, snp.id, snp.alleles[0].position)
                        self.add_snp_tuple(snp)
                        if self.snp_count % 100000 == 0:
                            print("Loaded %i snps. %s" % (self.snp_count, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                    else:
                        invalid_count += 1
                    # otherwise new snp row
                    snp = RefSNP.from_row_proxy(snp_row)

                # Added joined allele data every time
                snp.put_allele(Allele.from_row_proxy(snp_row))
                current_snp_id = snp_row["id"]
            # self.output_map_file_line(f, snp.chromosome, snp.id, snp.alleles[0].position)
            self.add_snp_tuple(snp)
        self.ordered_snps.sort(key=lambda x: x.chromosome)
        print("Skipped Invalid:        %i" % invalid_count)
        print("Total Loaded:           %i" % len(self.ordered_snps))

    def load_snps_json(self, directory, min_freq):
        """
        DEPRECATED
        Loads snps from passed in directory looking for files in json format. Loaded as RefSNP object then
        converted to a set of SNPTuples and output to a map file in the same order as the saved order.
        :param directory: Directory to load for RefSNP data in json format
        :param min_freq: Min frequency of the minor allele to be loaded. SNPs with a lower frequency will be
        filtered out
        :return: nothing
        """
        # Seems the entire refSNP db might be in the order of 400 million SNPs so filtering will be needed
        # 95% of mutations in any persons's genome are from common mutations (>1% odds), though.
        # We may need to shrink to only include a subset using a min frequency threshold

        snp_file_list = glob.glob(directory + "/*chr*.json*")
        for snp_file in snp_file_list:
            chrom_snps = {}
            chromosome = chromosome_from_filename(snp_file)

            open_fn = open
            if snp_file.endswith(".gz"):
                open_fn = gzip.open
            with open_fn(snp_file, 'rt') as f:
                indel_count = 0
                multi_nt_count = 0
                small_sample_count = 0
                low_freq_count = 0
                for line in f:
                    snp_dict = json.loads(line)
                    name = snp_dict["id"]
                    alleles = snp_dict.get("alleles")
                    if not alleles:
                        continue
                    # find the most common allele
                    max_allele_count = 0
                    total_count = 0
                    is_valid_for_plink = True
                    for allele in alleles:
                        if not allele['deleted'] or not allele['inserted']:
                            # Skip inserts, deletes
                            is_valid_for_plink = False
                            indel_count += 1
                            break
                        if len(allele['inserted']) > 1 or len(allele['deleted']) > 1:
                            # Skip multi-NT snps
                            multi_nt_count += 1
                            is_valid_for_plink = False
                            break
                        if allele['allele_count'] > max_allele_count:
                            max_allele_count = allele['allele_count']
                        total_count += allele['allele_count']
                    if total_count < 1000:
                        is_valid_for_plink = False
                        small_sample_count += 1
                    if not is_valid_for_plink:
                        continue
                    common_allele_freq = max_allele_count / total_count
                    if common_allele_freq <= (1 - min_freq):
                        # If passes freq filter, then save it
                        snp = RefSNP(name, chromosome)
                        for allele_attr in alleles:
                            allele = Allele(allele_attr['deleted'], allele_attr['inserted'],
                                            allele_attr['position'])
                            allele.allele_count = allele_attr['allele_count']
                            # Use summed total count because some refSNP data does not add up.
                            # Example with total larger than all counts https://www.ncbi.nlm.nih.gov/snp/rs28972095
                            allele.total_count = total_count
                            snp.put_allele(allele)
                        chrom_snps[snp.id] = snp
                    else:
                        low_freq_count += 1
            self.output_map_file(chromosome, chrom_snps.values())
            print("Loaded SNPs from %s" % snp_file)
            print("Skipped Indels:        %i" % indel_count)
            print("Skipped Small Sample:  %i" % small_sample_count)
            print("Skipped Multi-NT:      %i" % multi_nt_count)
            print("Skipped Freq Filtered: %i" % low_freq_count)
            print("Total Loaded:          %i" % len(chrom_snps))

    def output_map_file_line(self, outstream, chromo, snp_id, position):
        """
        Appends snps to a map file used by plink (snps).
        :param chromo: The chromosome these SNPs reside on
        :return: nothing
        """
        outstream.write("%s\trs%s\t0\t%s\n" % (chromo, snp_id, position))

    def add_snp_tuple(self, snp):
        """
        Adds snp to self.ordered_snps.
        self.ordered_snps is in the same order as the map file.
        One list per SNP that has a tuple per allele with the inserted value and probability range. For instance
        if a SNP has 3 alleles A (55%), T (25%), C (20%) the tuples would be ("A",0.55), ("T",0.8), ("C", 1.0)"""
        running_allele_count = 0
        snp_tuple = SNPTuples(snp.id, snp.chromosome, snp.alleles[0].position)
        snp.alleles.sort(key=lambda x: x.allele_count, reverse=True)
        # Insert tuples in sorted order by frequency desc
        for allele in snp.alleles:
            snp_tuple.add_tuple(allele.inserted,
                                (allele.allele_count + running_allele_count) / snp.total_count)
            running_allele_count += allele.allele_count

            # Save each chromosome separately, but in an ordered list of tuples so the line up with the map file
        self.snp_count += 1
        self.ordered_snps.append(snp_tuple)

    @classmethod
    def pick_pathogen_groups(cls, pathogen_groups, pop_size):
        return random.choices(
            population=list(pathogen_groups),
            weights=list(map(lambda x: x.population_weight, pathogen_groups)),
            k=pop_size
        )

    def generate_fam_file(self, control_size, test_size, male_odds, pathogen_group_list):
        """

        :param control_size:
        :param test_size:
        :param male_odds:
        :return: Data for each sample
        """
        control_id = 100000
        test_id = 500000
        randoms = numpy.random.rand(control_size + test_size)
        sample_data = []
        with open(self.population_dir + "population.fam", 'w') as f, \
                open(self.population_dir + "pop_pathogens.txt", "w") as pp:
            j = 0
            for i in range(control_size + test_size):
                is_control = i < control_size
                if is_control:
                    control_id += 1
                    iid = control_id
                else:
                    test_id += 1
                    iid = test_id
                if randoms[i] <= male_odds:
                    sex_code = 1
                else:
                    sex_code = 2

                if not is_control:
                    pathogen_group = pathogen_group_list[j]
                    j += 1
                    pathogen_snps = pathogen_group.select_mutations()
                    pp.write("%i\t%s\t" % (test_id, pathogen_group.name) +
                             "\t".join(map(lambda x: "rs" + str(x), pathogen_snps.keys())) + "\n")
                else:
                    pathogen_snps = None
                sample = SampleInfo(i + 1, iid, 0, 0, sex_code, is_control, pathogen_snps)
                sample_data.append(sample)
                f.write(sample.to_fam_format())

        return sample_data

    def output_vcf_population(self, control_size, test_size, male_odds):
        """
        Output a population .vcf file and companion .fam file.
        :param test_size: size of control group
        :param control_size: size of cases/test group
        :param male_odds: odds of a person being a biological male
        :return:
        """

        if not self.ordered_snps:
            raise Exception("No SNPs to Process! Exiting.")
        # pick pathogen groups for population size
        pathogen_group_list = PopulationFactory.pick_pathogen_groups(list(self.pathogens.values()), test_size)

        fam_data = self.generate_fam_file(control_size, test_size, male_odds, pathogen_group_list)
        main_file = self.population_dir + "population.vcf.gz"
        chromo_chunked_snps = []
        cur_chromo = self.ordered_snps[0].chromosome
        cur_list = []
        for snp in self.ordered_snps:
            if snp.chromosome != cur_chromo:
                chromo_chunked_snps.append(cur_list)
                cur_list = []
                cur_chromo = snp.chromosome
            cur_list.append(snp)
        chromo_chunked_snps.append(cur_list)
        with gzip.open(main_file, 'wt+', compresslevel=6) as f:
            write_vcf_header(f, fam_data)
            for snp_list in chromo_chunked_snps:
                self.write_vcf_snps(fam_data, snp_list, f)

        print("Finished VCF file output.")

    def write_vcf_snps(self, fam_data, snps, file, header=False):
        processes = []
        q = Queue(1000)
        # Create a process for each split group
        n_processes = self.num_processes
        if len(snps) < n_processes:
            # Small chunk of work so use 1 process
            n_processes = 1
        snp_chunks = list(split_list(snps, n_processes))
        for i in range(n_processes):
            p = Process(target=self.queue_vcf_snps, args=(fam_data, snp_chunks[i], q))
            processes.append(p)
            p.start()
        while any(p.is_alive() for p in processes):
            while not q.empty():
                line = q.get()
                file.write(line)

    @Timer(text="Finished VCF SNP output Elapsed time: {:0.4f} seconds", logger=print)
    def queue_vcf_snps(self, fam_data, snps, q):

        num_samples = len(fam_data)
        for snp_num, snp in enumerate(snps, start=1):
            sample_values = []
            # Roll the dice for each sample and each allele.
            randoms = numpy.random.rand(num_samples * 2)
            for i, sample in enumerate(fam_data):
                is_male = sample.is_male()

                if not is_male and snp.chromosome == 'Y':
                    # No Y chromosome for women
                    sample_values.append(".")
                if sample.is_control or snp.id not in sample.pathogen_snps:
                    random_roll = randoms[i * 2]
                    selected_nt = snp.pick_allele_index(random_roll)
                    if is_haploid(snp.chromosome, is_male):
                        sample_values.append(str(selected_nt))
                        continue
                    else:
                        random_roll = randoms[i * 2 + 1]
                        other_nt = snp.pick_allele_index(random_roll)
                    sample_values.append("%i/%i" % (selected_nt, other_nt))
                else:
                    if is_haploid(snp.chromosome, is_male):
                        sample_values.append("1")
                    else:
                        sample_values.append("1/1")
                    # TODO make it so pathogens can be recessive or dominant
            # Output row - CHROM, POS, ID, REF, ALT, QUAL FILTER, INFO, FORMAT, (SAMPLE ID ...)
            # 1      10583 rs58108140  G   A   25   PASS    .    GT     0/0     0/0     0/0
            line = "%s\t%i\trs%s\t%s\t%s\t40\tPASS\t.\tGT\t" % (snp.chromosome,
                                                                snp.position,
                                                                snp.id,
                                                                snp.ref_allele_tuple()[0],
                                                                snp.alt_alleles()) + \
                   "\t".join(sample_values) + "\n"
            q.put(line)
            if snp_num % 5000 == 0:
                print("Output %i/%i VCF lines in file." % (snp_num, len(snps)))

    def output_population(self, size, is_control, male_odds):
        """
        Output a population file of two nucleotide values per SNP. Correctly outputs duplicate values if the
        chromosome is haploid
        :param size: size of the generated population
        :param is_control: control population with no hidden pathogens
        :param male_odds: odds of a person being a biological male
        :return:
        """
        if not is_control:
            # pick pathogen groups for population size
            pathogen_group_list = PopulationFactory.pick_pathogen_groups(list(self.pathogens.values()), size)
            pathogen_snps = {}
        with open(self.population_dir + "population.ped", 'a+') as f, \
                open(self.population_dir + "pop_pathogens.txt", "a+") as pp:
            if is_control:
                row = 1000000
            else:
                row = 5000000
            for i in range(size):
                # Roll the dice for each snp and each allele. This will be a bit long for boys, but will work
                randoms = numpy.random.rand(self.snp_count * 2)
                is_male = randoms[0] < male_odds
                snp_values = []
                j = 0

                # If in test group... Select a pathogen group, then select pathogen snps.
                if not is_control:
                    pathogen_snps = pathogen_group_list[i].select_mutations()
                for snp in self.ordered_snps:
                    if not is_male and snp.chromosome == 'Y':
                        continue  # Skip Y snps for women
                    if is_control or snp.id not in pathogen_snps:
                        random_roll = randoms[j]
                        j += 1
                        selected_nt = snp.pick_snp_value(random_roll)
                        if is_haploid(snp.chromosome, is_male):
                            other_nt = selected_nt
                        else:
                            random_roll = randoms[j]
                            j += 1
                            other_nt = snp.pick_snp_value(random_roll)
                        snp_values.append(selected_nt)
                        snp_values.append(other_nt)
                    else:
                        selected_nt = snp.pick_pathogen_value()
                        snp_values.append(selected_nt)
                        snp_values.append(selected_nt)
                        # TODO make it so pathogens can be recessive or dominant
                # Output row - Family ID, Indiv ID, Dad ID, MomID, Sex, affection, snps
                if is_male:
                    sex = 1
                else:
                    sex = 2
                if is_control:
                    affection = 1
                else:
                    affection = 2
                f.write("\t".join(map(lambda x: str(x), [row, row, 0, 0, sex, affection])) + "\t" + "\t".join(
                    snp_values) + "\n")
                if not is_control:
                    pp.write("%i\t%s\t" % (row, pathogen_group_list[i].name) +
                             "\t".join(map(lambda x: "rs" + str(x), pathogen_snps.keys())) + "\n")
                row += 1
                if i % 100 == 0:
                    group_name = "Test"
                    if is_control:
                        group_name = "Control"
                    print("Output %i memebers of the %s group." % (i, group_name))

    def pick_pathogen_snps(self, snp_data, pathogens_config):
        """
        Pick and store the snps that are the pathogens. Randomly? pick num_mutations from the snps
        :param pathogens_config: file path for pathogens yaml file
        :param snp_data: SNPTuples which are candidates for being a pathogen
        :return: nothing... self.pathogens is populated
        """

        with open(pathogens_config, 'r') as p:
            pathogen_yml = load(p, Loader=Loader)
            for group, group_attr in pathogen_yml.items():
                iterations = 1
                if group_attr['num_instances']:
                    iterations = int(group_attr['num_instances'])
                for i in range(0, iterations):
                    path_group = PathogenGroup.from_yml(group_attr, snp_data, "%s-%s" % (group, i))
                    self.pathogens[path_group.name] = path_group
        with open(self.population_dir + "pathogens.txt", 'w') as f:
            for group_name, pathogen_group in self.pathogens.items():
                f.write(str(group_name) + ":\n")
                for snp_id, weight in pathogen_group.pathogens.items():
                    f.write("rs%s\t%s\n" % (snp_id, weight))


class PathogenGroup:

    def __init__(self, name, mutation_weights, snp_data, population_weight,
                 min_minor_allele_freq=0, max_minor_allele_freq=1.1):
        """
        Inits the pathogen dictionary to be random alleles matching the freq filters. pathogens dict
        stores snp.id => mutation weight mapping.
        :param mutation_weights: list of floats of the value each picked
        :param snp_data: snp dictionary
        :param population_weight: the weight this pathogen group has (shares in test population)
        """
        self.pathogens = {}
        self.name = name
        self.population_weight = population_weight

        filter_snps = min_minor_allele_freq > 0 or max_minor_allele_freq < 0.5
        filtered_list = snp_data
        if filter_snps:
            filtered_list = filter(
                lambda x: min_minor_allele_freq <= (x.minor_allele_tuple()[1]
                                                    - x.ref_allele_tuple()[1]) <= max_minor_allele_freq,
                filtered_list)
        snp_id_list = list(map(lambda x: x.id, filtered_list))
        if len(snp_id_list) == 0:
            raise Exception("All SNPs filtered out. No snps match pathogen filter %f <= freq <= %f" %
                            (min_minor_allele_freq, max_minor_allele_freq))
        i = 0
        for snp_id in numpy.random.choice(a=snp_id_list, size=len(mutation_weights), replace=False):
            self.pathogens[snp_id] = mutation_weights[i]
            i += 1

    @classmethod
    def from_yml(cls, yml_attr, snp_data, name):
        min_minor_allele_freq = 0
        max_minor_allele_freq = 1
        if yml_attr.get('min_minor_allele_freq'):
            if 0 < yml_attr['min_minor_allele_freq'] < 0.5:
                min_minor_allele_freq = yml_attr['min_minor_allele_freq']
            else:
                raise Exception('min_minor_allele_freq must be between 0 and 0.5. yml value = {}'.format(
                    yml_attr['min_minor_allele_freq']))
        if yml_attr.get('max_minor_allele_freq'):
            if 0 < yml_attr['max_minor_allele_freq'] < 0.5:
                max_minor_allele_freq = yml_attr['max_minor_allele_freq']
            else:
                raise Exception('max_minor_allele_freq must be between 0 and 0.5. yml value = {}'.format(
                    yml_attr['max_minor_allele_freq']))

        return cls(name, yml_attr['mutation_weights'], snp_data, yml_attr['population_weight'],
                   min_minor_allele_freq, max_minor_allele_freq)

    def select_mutations(self):
        """
        Randomly select mutations a single individual might have if they are in this PathogenGroup
        :return: a colleciton of snp_ids that are mutated
        """
        selected_pathogens = {}
        shuffled_pathogens = list(self.pathogens.items())
        random.shuffle(shuffled_pathogens)  # Shuffle to randomly select
        agg_weight = 0
        for p in shuffled_pathogens:
            selected_pathogens[p[0]] = p[1]
            agg_weight += p[1]  # sum the weights
            if agg_weight >= 1:
                break
        return selected_pathogens


def print_help():
    print("""
    Accepted Inputs are:
    -s size of test group (afflicted group)
    -c size of control group
    -f min frequency for a SNP to be included in the list of SNPs, default is 0.005
    -p location of pathogens config yaml file (default is pathogens.yml in working dir)
    -m odds of a population member being male (default 0.5)
    -x max number of snps to use 
    """)


def main(argv):
    try:
        opts, args = getopt.getopt(argv, "h?p:f:s:c:x:n:", ["help"])
    except getopt.GetoptError as err:
        print(err.msg)
        print_help()
        sys.exit(2)
    min_freq = MIN_SNP_FREQ
    male_odds = 0.5
    max_snps = 10000000
    pathogens_file = 'pathogens.yml'
    snp_dir = SNP_DIR
    num_processes = 1
    for opt, arg in opts:
        if opt in ('-h', "-?", "--help"):
            print_help()
            sys.exit()
        elif opt in "-p":
            pathogens_file = arg
        elif opt in "-s":
            size = int(arg)
        elif opt in "-c":
            control_size = int(arg)
        elif opt in "-f":
            min_freq = float(arg)
        elif opt in "-m":
            male_odds = float(arg)
        elif opt in "-x":
            max_snps = int(arg)
        elif opt in "-n":
            num_processes = int(arg)
    pop_factory = PopulationFactory(num_processes)
    pop_factory.generate_population(control_size, size, male_odds, pathogens_file, min_freq, max_snps)


if __name__ == '__main__':
    db.default_init()
    main(sys.argv[1:])
