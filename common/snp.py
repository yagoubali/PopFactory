"""
Common classes used by different python functions
"""

import json
import re
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import Column, Integer, Float, String, ForeignKey, orm
from sqlalchemy.orm import relationship
Base = declarative_base()


def chromosome_from_filename(filename):
    chr_search = re.search('chr([0-9XYMT]+)', filename, re.IGNORECASE)
    if chr_search:
        return chr_search.group(1)
    else:
        return 'unknown'


def is_haploid(chromo, is_male):
    """
    Is this chromosome haploid (one allele per person)
    :param chromo: chromosome letter or number (X, Y, MT or 1-22)
    :param is_male: boolean if male
    :return:
    """
    return (chromo == 'X' and is_male) or chromo == 'MT' or chromo == 'Y'


class Allele(Base):
    """
    refsnp -> allele
    id        (id, refsnp_id, deleted, inserted, seq_id, position, allele_count)
    chromo
    MAF
    total_count
    """
    __tablename__ = "alleles"

    id = Column(Integer, primary_key=True)
    deleted = Column(String)
    inserted = Column(String)
    position = Column(Integer)
    allele_count = Column(Integer, index=True)
    ref_snp_id = Column(Integer, ForeignKey('ref_snps.id'), nullable=False)

    def __init__(self, deleted, inserted, position):
        self.name = Allele.name_string(deleted, inserted)
        self.deleted = deleted
        self.inserted = inserted
        self.position = position
        self.allele_count = 0
        self.total_count = 0

    @orm.reconstructor
    def init_on_load(self):
        self.name = Allele.name_string(self.deleted, self.inserted)

    def add_observation(self, allele_count, total_count):
        self.allele_count += int(allele_count)
        self.total_count += int(total_count)

    def freq(self):
        return self.allele_count / self.total_count

    def to_dict(self):
        return {"deleted": self.deleted,
                "inserted": self.inserted,
                "position":  self.position,
                "seq_id": self.seq_id,
                "allele_count": self.allele_count,
                "total_count": + self.total_count}

    @classmethod
    def from_dict(cls, attr_dict):
        a = cls(attr_dict["deleted"], attr_dict["inserted"], attr_dict["position"])
        a.allele_count = attr_dict["allele_count"]
        a.total_count = attr_dict["total_count"]
        return a

    @staticmethod
    def name_string(deleted, inserted):
        return deleted + "->" + inserted


class RefSNP(Base):
    __tablename__ = "ref_snps"

    id = Column(Integer, primary_key=True)
    chromosome = Column(String)
    maf = Column(Float, index=True)
    total_count = Column(Integer, index=True)
    alleles = relationship("Allele")

    def __init__(self, ref_id):
        self.id = ref_id
        self.alleles = []

    def put_allele(self, allele):
        self.alleles.append(allele)

    @classmethod
    def from_json(cls, json_line, chromosome):
        ref_obj = json.loads(json_line)
        ref_snp = cls(ref_obj['id'])
        ref_snp.chromosome = str(chromosome)
        for a in ref_obj['alleles']:
            allele = Allele.from_dict(a)
            ref_snp.put_allele(allele)
        return ref_snp

    @classmethod
    def from_nih_json(cls, json_line):
        ref_obj = json.loads(json_line)
        ref_snp = cls(ref_obj['refsnp_id'])
        if 'primary_snapshot_data' in ref_obj:
            placements = ref_obj['primary_snapshot_data']['placements_with_allele']

            for alleleinfo in placements:
                placement_annot = alleleinfo['placement_annot']
                if alleleinfo['is_ptlp'] and \
                        len(placement_annot['seq_id_traits_by_assembly']) > 0:
                    ref_snp.assembly_name = placement_annot[
                        'seq_id_traits_by_assembly'][0]['assembly_name']

                    for a in alleleinfo['alleles']:
                        spdi = a['allele']['spdi']
                        allele = Allele(spdi['deleted_sequence'],
                                        spdi['inserted_sequence'],
                                        spdi['position'])
                        ref_snp.put_allele(allele)
            for allele_annotation in ref_obj['primary_snapshot_data']['allele_annotations']:
                for freq in allele_annotation['frequency']:
                    obs = freq['observation']
                    name = Allele.name_string(obs['deleted_sequence'],
                                              obs['inserted_sequence'])
                    for allele in ref_snp.alleles:
                        if name == allele.name:
                            allele.add_observation(freq['allele_count'], freq['total_count'])
        return ref_snp

    def total_allele_count(self):
        sum_count = 0
        for a in self.alleles:
            sum_count += a.allele_count
        return sum_count

    def __str__(self):
        json_hash = {"id": self.id}
        if len(self.alleles) > 0:
            json_hash["alleles"] = []
        for allele in self.alleles:
            json_hash["alleles"].append(allele.to_dict)
        return json.dumps(json_hash)

