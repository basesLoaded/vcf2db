"""
Take a VCF and create a gemini compatible database
"""
from __future__ import print_function
import sys

import itertools as it
import re
import zlib
import snappy
try:
    import cPickle as pickle
except ImportError:
    import pickle

import time
from collections import defaultdict
import multiprocessing

import numpy as np
import sqlalchemy as sql
from peddy import Ped
import geneimpacts
import cyvcf2

import cProfile
try:
    import StringIO
except ImportError:
    import io as StringIO

import pstats
import contextlib

GT_TYPE_LOOKUP = {
        'gt_depths': sql.Integer,
        'gt_ref_depths': sql.Integer,
        'gt_alt_depths': sql.Integer,
        'gt_quals': sql.Float,
        'gt_types': sql.SmallInteger,
        }

def fix_sample_name(s, patt=re.compile('-|\s|\\\\')):
    if s in ('0', '-9'): return s
    return patt.sub("_", s)

def grouper(n, iterable):
    iterable = iter(iterable)
    piece = list(it.islice(iterable, n))
    while piece:
        yield piece
        piece = list(it.islice(iterable, n))

@contextlib.contextmanager
def profiled():
    pr = cProfile.Profile()
    pr.enable()
    yield
    pr.disable()
    s = StringIO.StringIO()
    ps = pstats.Stats(pr, stream=s).sort_stats('time')
    ps.print_stats(60)
    # uncomment this to see who's calling what
    # ps.print_callers()
    print(s.getvalue())

def set_column_length(e, column, length, saved={}):
    table = column.table
    c = column.table.columns[column.name]
    if c.type.length >= length:
        return
    c.type.length = length
    column.type.length = length
    if saved.get((table.name, c.name), 0) < length:
        sys.stderr.write("changing varchar field '%s' to length %d\n" %
                                     (c.name,  length))
    saved[(table.name, c.name)] = c.type.length
    if e.dialect.name.startswith("postgres"):
        e.execute('ALTER TABLE %s ALTER COLUMN %s TYPE VARCHAR(%d)' %
                            (table.name, c.name, length))
    elif e.dialect.name == "mysql":
        e.execute('ALTER TABLE %s MODIFY %s VARCHAR(%d)' %
                                (table.name, c.name, length))

# THIS snappy code is copied from gemini. do not change here.
# we use the numpy type char as the first item we save to know the dtype when we decompress.
SEP = '\0'
def snappy_pack_blob(obj, sep=SEP):
    if obj is None: return ''
    c = obj.dtype.char
    if c == 'S': return 'S' + snappy.compress(sep.join(obj))
    return buffer(c + snappy.compress(obj.tobytes()))

def pack_blob(obj, _none=zlib.compress(pickle.dumps(None, pickle.HIGHEST_PROTOCOL))):
    if obj is None: return _none
    return zlib.compress(pickle.dumps(obj, pickle.HIGHEST_PROTOCOL), 1)


def clean(name):
    """
    turn a vcf id into a db name
    """
    return name.replace("-", "_").replace(" ", "_").strip('"').strip("'").lower()

def info_parse(line,
               _patt=re.compile("(\w+)=(\"[^\"]+\"|[^,]+)")):
    """
    >>> ret = info_parse('##INFO=<ID=AC,Number=A,Type=Integer,Description="Allele count in genotypes, for each ALT allele, in the same order as listed">')
    >>> assert ret == {"ID": "AC", "Number": "A", "Type": "Integer","Description": '"Allele count in genotypes, for each ALT allele, in the same order as listed"'}, ret
    """
    assert line.startswith("##INFO=")
    stub = line.split("=<")[1].rstrip(">")
    return dict(_patt.findall(stub))


type_lookups = {
        "Integer": sql.Integer(),
        "Float": sql.Float(),
        "Flag": sql.Boolean(),
        "Character": sql.String(1),
        "String": sql.String(5),
        }

def get_url(db_path):
    if not db_path.startswith(("sqlite:", "mysql", "postgres")):
        db_path = "sqlite:///" + db_path
    return db_path

class VCFDB(object):
    gt_cols = ("gts", "gt_types", "gt_phases", "gt_depths", "gt_ref_depths",
               "gt_alt_depths", "gt_quals")

    effect_list = ["CSQ", "ANN", "EFF"]
    _black_list = []

    def __init__(self, vcf_path, db_path, ped_path=None, blobber=pack_blob,
                 black_list=None, expand=None):
        self.vcf_path = vcf_path
        self.db_path = get_url(db_path)
        self.engine = sql.create_engine(self.db_path, poolclass=sql.pool.NullPool)
        self.impacts_headers = {}
        self.metadata = sql.MetaData(bind=self.engine)
        self.expand = expand or []

        self.blobber = blobber
        self.ped_path = ped_path
        self.black_list = list(VCFDB._black_list) + list(VCFDB.effect_list) + (black_list or [])
        self.pool = multiprocessing.Pool(2)

        self.vcf = cyvcf2.VCF(vcf_path)
        # we use the cache to infer the lengths of string fields.
        self.cache = it.islice(self.vcf, 10000)
        self.create_columns()
        self.samples = self.create_samples()
        self.load()
        self.index()

    def _set_variant_properties(self, v, d):
        d['type'] = v.var_type
        d['sub_type'] = v.var_subtype
        d['call_rate'] = v.call_rate
        d['num_hom_ref'] = v.num_hom_ref
        d['num_het'] = v.num_het
        d['num_hom_alt'] = v.num_hom_alt
        d['aaf'] = v.aaf

    def _load(self, iterable, create, start):

        variants = []
        expanded = {k: [] for k in self.expand}
        keys = set()
        i = None
        for i, v in enumerate(iterable, start=start):
            d = dict(v.INFO)
            for c in self.gt_cols:
                # named gt_bases in cyvcf2 and gts in db
                arr = v.gt_bases if c == "gts" else getattr(v, c, None)
                if arr is not None:
                    arr = arr[self.sample_idxs]
                d[c] = arr

            d['chrom'], d['start'], d['end'] = v.CHROM, v.start, v.end
            d['ref'], d['alt'] = v.REF, ",".join(v.ALT)

            d['qual'], d['filter'], d['vcf_id'] = v.QUAL, v.FILTER, v.ID
            d['variant_id'] = i
            self._set_variant_properties(v, d)

            for k in self.expand:
                arr = d[k].tolist() # need to convert to list or we get np types
                e = {'sample_' + s: arr[k] for k, s in enumerate(self.samples)}
                e['variant_id'] = d['variant_id']
                expanded[k].append(e)

            # TODO: just save required keys outside.
            keys.update(d.keys())

            variants.append(d)
            # http://docs.sqlalchemy.org/en/latest/faq/performance.html
            if not create and (i % 10000) == 0:
                self.insert(variants, expanded, keys, i)
                variants = variants[:0]
                for k in expanded:
                    expanded[k] = expanded[k][:0]

        if len(variants) != 0:
            self.insert(variants, expanded, keys, i, create=create)

        return i

    def load(self):
        self.t0 = self.t = time.time()

        i = self._load(self.cache, create=True, start=1)
        self.cache = []
        #with profiled():
        self._load(self.vcf, create=False, start=i+1)

    def check_column_lengths(self, dicts, cols):
        change_cols = defaultdict(int)
        for name, c in cols.items():
            l = c.type.length
            for d in dicts:
                if len(d.get(name) or '') > l:
                    change_cols[c.name] = max(change_cols[c.name], len(d.get(name)))
        return dict(change_cols)

    def insert(self, variants, expanded, keys, i, create=False):
        ivariants, variant_impacts = [], []
        te = time.time()
        for variant, impacts in it.imap(gene_info, ((v,
        #for variant, impacts in self.pool.imap(gene_info, ((v,
                     self.impacts_headers, self.blobber, self.gt_cols, keys) for
                     v in variants)
                     ):
                     #, 20):
            variant_impacts.extend(impacts)
            ivariants.append(variant)
        te = time.time() - te

        variants = ivariants
        vlengths = vilengths = {}

        if create:
            self.create(variants, variant_impacts)

        elif self.engine.dialect.name != "sqlite":
            vlengths = self.check_column_lengths(variants, {c.name: c for c in self.variants_columns if
                c.type.__class__.__name__ == "String"})

            vilengths = self.check_column_lengths(variant_impacts, {c.name: c for c in
                self.variant_impacts_columns if c.type.__class__.__name__ ==
                "String"})

        self._insert(vlengths, variants,
                     vilengths, variant_impacts)

        ex = time.time()
        for k in expanded:
            self.__insert(expanded[k], self.metadata.tables["sample_" + k].insert())
        ex = time.time() - ex
        vps = i / float(time.time() - self.t0)

        # reduce number of error messages after 100K
        if i <= 100000 or i % 200000 == 0:

            fmt = "%d variant_impacts:%d\teffects time: %.1f\tchunk time:%.1f\t%.2f variants/second"
            if self.expand:
                fmt += "\texpanded columns:%.2f\n"
                sys.stderr.write(fmt % (i, len(variant_impacts), te, time.time() - self.t, vps, ex))
            else:
                fmt += "\n"
                sys.stderr.write(fmt % (i, len(variant_impacts), te, time.time() - self.t, vps))

        self.t = time.time()


    def _insert(self, vlengths, v_objs, vilengths, vi_objs):

        for name, clen in vlengths.items():
            col = self.variants.columns[name]
            set_column_length(self.engine, col, clen)

        self.__insert(v_objs, self.metadata.tables['variants'].insert())

        for name, clen in vilengths.items():
            col = self.variant_impacts.columns[name]
            set_column_length(self.engine, col, clen)

        self.__insert(vi_objs, self.metadata.tables['variant_impacts'].insert())


    def __insert(self, objs, stmt):

        tx = time.time()
        # (2006, 'MySQL server has gone away'
        # if you see this, need to increase max_allowed_packet and/or other
        # params in my.cnf (or we should detect and reduce the chunk size)
        if len(objs) > 6000:
            for group in grouper(5000, objs):
                g = list(group)
                try:
                    self.engine.execute(stmt, g)
                except:
                    self.engine.rollback()
                    with self.engine.begin() as trans:
                        for o in g:
                            trans.execute(stmt, o)
                    raise
        else:
            try:
                self.engine.execute(stmt, objs)
            except:
                with self.engine.begin() as trans:
                    for o in objs:
                        trans.execute(stmt, o)
                raise
        return time.time() - tx

    def create_columns(self):
        self.variants_columns = self.get_variants_columns()
        self.variant_impacts_columns = self.get_variant_impacts_columns()

    def create(self, dvariants, dvariant_impacts):
        # update the lengths of the string columns based on the variants that
        # we've seen so far
        v_cols = {c.name: c for c in self.variants_columns if c.type.__class__.__name__ == "String"}
        self._create(dvariants, v_cols)

        vi_cols = {c.name: c for c in self.variant_impacts_columns if c.type.__class__.__name__ == "String"}
        self._create(dvariant_impacts, vi_cols)

        self._create_tables()

    def _create(self, dicts, cols):
        exclude_cols = set()
        for name, col in cols.items():
            if name in exclude_cols: continue
            for d in dicts:
                try:
                    if col.type.length < len(str(d.get(name, ''))):
                        # col.type.length = int(1.618 * len(d[name]) + 0.5)
                        col.type.length = int(1.2 * len(d[name]) + 0.5)
                except:
                    print(name, col.type.length)
                    raise
                if col.type.length > 48:
                    col.type = sql.TEXT()
                    exclude_cols.add(name)
                    break

    def _create_tables(self):
        self.variant_impacts = sql.Table("variant_impacts", self.metadata, *self.variant_impacts_columns)
        self.variant_impacts.drop(checkfirst=True)

        self.variants = sql.Table("variants", self.metadata, *self.variants_columns)
        self.variants.drop(checkfirst=True)

        # features table so gemini knows we're using snappy.
        if self.blobber == snappy_pack_blob:
            t = sql.Table("features", self.metadata,
                          sql.Column("feature", sql.String(20)))
            t.drop(checkfirst=True)
            t.create()
            self.engine.execute(t.insert(), {"feature": "snappy_compression"})

        self.variants.create()
        self.variant_impacts.create()
        self.create_vcf_header_table()
        self.create_expanded()

    def create_expanded(self):
        """
        We store the sample fields, e.g. depths and genotypes in a serialized
        blob but the user can also request --expand [] to have these put into
        separate tables for easier genotype-based querying
        """
        for field in self.expand:
            sql_type = GT_TYPE_LOOKUP[field]
            name = "sample_%s" % field
            cols = [sql.Column('variant_id', sql.Integer,
                               sql.ForeignKey('variants.variant_id'),
                               nullable=False, primary_key=False)]
            cols.extend([sql.Column("sample_" + s, sql_type, index=True) for s in self.samples])
            t = sql.Table(name, self.metadata, *cols)
            t.drop(self.engine, checkfirst=True)
            t.create()

    def create_vcf_header_table(self):
        h = self.vcf.raw_header
        t = sql.Table("vcf_header", self.metadata,
                      #sql.Column("vcf_header", sql.TEXT(len(h)))
                      sql.Column("vcf_header", sql.TEXT)
                      )
        t.drop(self.engine, checkfirst=True)
        t.create()
        self.engine.execute(t.insert(), [dict(vcf_header=h.rstrip())])

    def get_variant_impacts_columns(self):
        return [sql.Column("variant_id", sql.Integer,
                           sql.ForeignKey("variants.variant_id"), nullable=False),
                ] + self.variants_gene_columns()

    def index(self):
        sys.stderr.write("indexing ... ")
        t0 = time.time()
        sql.Index("idx_variants_chrom_start", self.variants.c.chrom, self.variants.c.start).create()
        sql.Index("idx_variants_exonic", self.variants.c.is_exonic).create()
        sql.Index("idx_variants_coding", self.variants.c.is_coding).create()
        sql.Index("idx_variants_impact", self.variants.c.impact).create()
        sql.Index("idx_variants_impact_severity", self.variants.c.impact_severity).create()
        sys.stderr.write("finished in %.1f seconds...\n" % (time.time() - t0))
        sys.stderr.write("total time: in %.1f seconds...\n" % (time.time() - self.t0))

    def create_samples(self):
        p = Ped(self.ped_path)
        samples = [fix_sample_name(s) for s in self.vcf.samples]
        cols = ['sample_id', 'family_id', 'name', 'paternal_id', 'maternal_id', 'sex', 'phenotype']
        idxs = []
        rows = []
        cols.extend(p.header[6:])
        for i, s in enumerate(p.samples(), start=1):
            try:
                idxs.append(samples.index(fix_sample_name(s.sample_id)))
            except ValueError:
                print("%s not in VCF" % s.sample_id, file=sys.stderr, end=' ')
                continue
            rows.append([i, s.family_id,
                         fix_sample_name(s.sample_id),
                         fix_sample_name(str(s.paternal_id)),
                         fix_sample_name(str(s.maternal_id)),
                         '1' if s.sex == 'male' else '2' if s.sex == 'female' else '-9',
                         '2' if s.affected is True else '1' if s.affected is False else '-9',
                ] + s.attrs)

        print(file=sys.stderr)
        scols = [sql.Column('sample_id', sql.Integer, primary_key=True)]
        for i, col in enumerate(cols[1:], start=1):
            vals = [r[i] for r in rows]
            l = max(len(v) for v in vals)
            scols.append(sql.Column(col, sql.String(l)))

        t = sql.Table('samples', self.metadata, *scols)
        t.drop(checkfirst=True)
        t.create()

        self.engine.execute(t.insert(), [dict(zip(cols, r)) for r in rows])

        # track the order to pull from the genotype fields.
        self.sample_idxs = np.array(idxs)
        return [r[2] for r in rows]

    def get_variants_columns(self):
        columns = self.variants_default_columns()
        columns.extend(self.variants_calculated_columns())
        columns.extend(self.variants_gene_columns())
        columns.extend(self.variants_sv_columns())
        columns.extend(self.variants_info_columns(self.vcf.raw_header))
        columns.extend(self.variants_genotype_columns())
        return columns

    def variants_default_columns(self):
        return [
            sql.Column("variant_id", sql.Integer(), primary_key=True),
            sql.Column("chrom", sql.String(10)),
            sql.Column("start", sql.Integer()),
            sql.Column("end", sql.Integer()),
            sql.Column("vcf_id", sql.String(12)),
            #sql.Column("anno_id", Integer()),
            sql.Column("ref", sql.TEXT()),
            sql.Column("alt", sql.TEXT()),
            sql.Column("qual", sql.Float()),
            sql.Column("filter", sql.String(10)),
           ]

    def variants_gene_columns(self):
        # all of these are also stored in the variant_impacts table.
        return [
            sql.Column("gene", sql.String(20)),
            sql.Column("transcript", sql.String(20)),
            sql.Column("is_exonic", sql.Boolean()),
            sql.Column("is_coding", sql.Boolean()),
            sql.Column("is_lof", sql.Boolean()),
            sql.Column("is_splicing", sql.Boolean()),
            sql.Column("exon", sql.String(8)),
            sql.Column("codon_change", sql.TEXT()),
            sql.Column("aa_change", sql.TEXT()),
            sql.Column("aa_length", sql.String(8)),
            sql.Column("biotype", sql.String(50)),
            sql.Column("impact", sql.String(20)),
            sql.Column("impact_so", sql.String(20)),
            sql.Column("impact_severity", sql.String(4)),
            sql.Column("polyphen_pred", sql.String(20)),
            sql.Column("polyphen_score", sql.Float()),
            sql.Column("sift_pred", sql.String(20)),
            sql.Column("sift_score", sql.Float()),
            ]


    def variants_calculated_columns(self):
        return [
            sql.Column("type", sql.String(8)),
            sql.Column("sub_type", sql.String(20)),
            sql.Column("call_rate", sql.Float()),
            sql.Column("num_hom_ref", sql.Integer()),
            sql.Column("num_het", sql.Integer()),
            sql.Column("num_hom_alt", sql.Integer()),
            sql.Column("aaf", sql.Float()),
            sql.Column("hwe", sql.Float()),
            sql.Column("inbreeding_coef", sql.Float()),
            sql.Column("pi", sql.Float()),
           ]

    def variants_sv_columns(self):
        return [
            #sql.Column('sv_cipos_start_left', Integer()),
            #sql.Column('sv_cipos_end_left', Integer()),
            #sql.Column('sv_cipos_start_right', Integer()),
            #sql.Column('sv_cipos_end_right', Integer()),
            #sql.Column('sv_length', Integer()),
            #sql.Column('sv_is_precise', Integer()),
            #sql.Column('sv_tool', String(20)),
            #sql.Column('sv_evidence_type', String(20)),
            #sql.Column('sv_event_id', String(20)),
            #sql.Column('sv_mate_id', String(20)),
            #sql.Column('sv_strand', String(1)),
               ]

    def variants_genotype_columns(self):
        return [sql.Column(name, sql.LargeBinary()) for name in self.gt_cols]

    def update_impacts_headers(self, hdr_dict):
        "keep the description so we know how to parse the CSQ/ANN fields"

        desc = hdr_dict["Description"]
        if hdr_dict["ID"] == "ANN":
            parts = [x.strip("\"'") for x in re.split("\s*\|\s*", desc.split(":", 1)[1].strip('" '))]
        elif hdr_dict["ID"] == "EFF":
            parts = [x.strip(" [])'(\"") for x in re.split("\||\(", desc.split(":", 1)[1].strip())]
        elif hdr_dict["ID"] == "CSQ":
            parts = [x.strip(" [])'(\"") for x in re.split("\||\(", desc.split(":", 1)[1].strip())]
        else:
            raise Exception("don't know how to use %s as annotation" % hdr_dict["ID"])
        self.impacts_headers[hdr_dict["ID"]] = parts

    def variants_info_columns(self, raw_header):
        "create Column() objects for each entry in the info field"
        for l in (x.strip() for x in raw_header.split("\n")):
            if not l.startswith("##INFO"):
                continue

            d = info_parse(l)
            if d["ID"] in self.effect_list:
                self.update_impacts_headers(d)
                continue

            id = clean(d["ID"])
            if d["ID"] in self.black_list or id in self.black_list:
                continue
            if id == "id":
                id = "idx"

            if id.endswith(("_af", "_aaf")) or id.startswith(("af_", "aaf_", "an_")):
                c = sql.Column(id, sql.Float(), default=-1.0)
            else:
                c = sql.Column(id, type_lookups[d["Type"]], primary_key=False)
            yield c

class noner(object):
    def __getattr__(self, key):
        return None

noner = noner()

def gene_info(d_and_impacts_headers):
    # this is parallelized as it's only simple objects and the gene impacts
    # stuff is slow.
    d, impacts_headers, blobber, gt_cols, req_cols = d_and_impacts_headers
    impacts = []
    for k in (eff for eff in ("CSQ", "ANN", "EFF") if eff in d):
        if k == "CSQ":
            impacts.extend(geneimpacts.VEP(e, impacts_headers[k], checks=False) for e in d[k].split(","))
        elif k == "ANN":
            impacts.extend(geneimpacts.SnpEff(e, impacts_headers[k]) for e in d[k].split(","))
        elif k == "EFF":
            impacts.extend(geneimpacts.OldSnpEff(e, impacts_headers[k]) for e in d[k].split(","))
        del d[k] # save some memory

    top = geneimpacts.Effect.top_severity(impacts)
    if isinstance(top, list):
        top = top[0]
    elif top is None:
        top = noner

    keys = ('gene', 'transcript', 'is_exonic', 'is_coding', 'is_splicing',
            'is_lof', 'exon', 'codon_change', 'aa_change', 'aa_length',
            'biotype', 'top_consequence', 'so', 'effect_severity',
            'polyphen_pred', 'polyphen_score', 'sift_pred', 'sift_score')


    for k in keys:
        d[k] = getattr(top, k)

    d['impact'] = top.top_consequence
    d['impact_so'] = top.so
    d['impact_severity'] = top.effect_severity

    for c in gt_cols:
        d[c] = blobber(d[c])

    # add what we need.
    u = dict.fromkeys(req_cols)
    u.update(d)
    for k in (rc for rc in req_cols if not rc.islower()):
        u[k.lower()] = d.get(k)
    d = u

    gimpacts = []
    for impact in impacts:
        #gimpacts.append({k: getattr(impact, k) for k in keys})
        gimpacts.append(dict(variant_id=d['variant_id'],
                             gene=impact.gene, transcript=impact.transcript,
                             is_exonic=impact.is_exonic, is_coding=impact.is_coding,
                             is_splicing=impact.is_splicing, is_lof=impact.is_lof,
                             exon=impact.exon, codon_change=impact.codon_change,
                             aa_change=impact.aa_change, aa_length=impact.aa_length,
                             biotype=impact.biotype, top_consequence=impact.top_consequence,
                             so=impact.so, effect_severity=impact.effect_severity,
                             polyphen_pred=impact.polyphen_pred,
                             polyphen_score=impact.polyphen_score,
                             sift_pred=impact.sift_pred,
                             sift_score=impact.sift_score))
    return d, gimpacts

if __name__ == "__main__":

    import doctest
    doctest.testmod()

    import argparse
    p = argparse.ArgumentParser(__doc__)
    p.add_argument("VCF")
    p.add_argument("ped")
    p.add_argument("db")
    p.add_argument("-e", "--info-exclude", action='append',
                   help="don't save this field to the database. May be specified " \
                        "multiple times.")
    p.add_argument("--legacy-compression", action='store_true', default=False)

    p.add_argument("--expand",
                   action='append',
                   default=[],
                   help="sample columns to expand into their own tables",
                   choices=GT_TYPE_LOOKUP.keys())



    a = p.parse_args()

    blobber = pack_blob if a.legacy_compression else snappy_pack_blob

    v = VCFDB(a.VCF, a.db, a.ped, black_list=a.info_exclude, expand=a.expand,
              blobber=blobber)
