"""Parse output of QC tools."""
import os
import csv
import logging
import subprocess
import pandas as pd

from typing import Any, Dict
from click.types import File

from ..models.qc import PostAlignQcResult, QcMethodIndex, QcSoftware, QuastQcResult

LOG = logging.getLogger(__name__)

class QC:
    """Class for retrieving qc results"""
    def __init__(self, sample_id, bam, reference, cpus, bed: str = None, baits: str = None):
        self.results = {}
        self.bam = bam
        self.bed = bed
        self.sample_id = sample_id
        self.cpus = cpus
        self.baits = baits
        self.reference = reference
        self.paired = self.is_paired()

    def write_json_result(self, json_result, output_filepath):
        """Write out json file"""
        with open(output_filepath, 'w', encoding="utf-8") as json_file:
            json_file.write(json_result)

    def convert2intervals(self, bed_baits, dict_file):
        """Convert files to interval lists"""
        bed2int_cmd = ["java", "-jar", "/usr/bin/picard.jar", "BedToIntervalList", "-I", bed_baits, "-O", f"{bed_baits}.interval_list", "-SD", dict_file]
        self.system_p(bed2int_cmd)

    def parse_hsmetrics(self, hsmetrics):
        """Parse hs metrics"""
        with open(hsmetrics, "r", encoding="utf-8") as fin:
            for line in fin:
                if line.startswith("## METRICS CLASS"):
                    next(fin)
                    vals = next(fin).split("\t")
                    self.results['pct_on_target'] = vals[18]
                    self.results['fold_enrichment'] = vals[26]
                    self.results['median_coverage'] = vals[23]
                    self.results['fold_80'] = vals[33]

    def parse_ismetrics(self, ismetrics):
        """Parse insert size metrics"""
        with open(ismetrics, "r", encoding="utf-8") as ins:
            for line in ins:
                if line.startswith("## METRICS CLASS"):
                    next(ins)
                    vals = next(ins).split("\t")
                    self.results['ins_size'] = vals[5]
                    self.results['ins_size_dev'] = vals[6]

    def parse_basecov_bed(self, basecov_fpath, thresholds):
        """Parse base coverage bed file using pandas"""
        df = pd.read_csv(basecov_fpath, sep='\t', comment='#', header=0)

        tot_bases = len(df)
        pct_above = {min_val: len(df[df['COV'] >= int(min_val)]) for min_val in thresholds}
        pct_above = {min_val: 100 * (pct_above[min_val] / tot_bases) for min_val in thresholds}

        mean_cov = df['COV'].mean()

        # Calculate the inter-quartile range / median (IQR/median)
        quartile1 = df['COV'].quantile(0.25)
        median = df['COV'].median()
        quartile3 = df['COV'].quantile(0.75)

        iqr_median = quartile3 - quartile1 if quartile1 and quartile3 and median else None

        self.results['pct_above_x'] = pct_above
        self.results['mean_cov'] = mean_cov
        self.results['iqr_median'] = iqr_median
        self.results['quartile1'] = quartile1
        self.results['median'] = median
        self.results['quartile3'] = quartile3

    def is_paired(self):
        """Check if reads are paired"""
        line = subprocess.check_output(f"samtools view {self.bam} | head -n 1| awk '{{print $2}}'", shell=True, text=True)
        remainder = int(line) % 2
        return bool(remainder)

    def system_p(self, cmd):
        """Execute subproces"""
        LOG.info("RUNNING: %s", ' '.join(cmd))
        result = subprocess.run(cmd, check=True, text=True)
        if result.stderr:
            print(f"stderr: {result.stderr}")
        if result.stdout:
            print(f"stdout: {result.stdout}")

    def run(self) -> dict:
        """Run QC info extraction"""
        if self.baits and self.reference:
            LOG.info("Calculating HS-metrics...")
            dict_file = self.reference
            if not dict_file.endswith(".dict"):
                dict_file += ".dict"

            # Convert bed/baits file to interval list
            if not os.path.isfile(f"{self.bed}.interval_list"):
                self.convert2intervals(self.bed, dict_file)
            if not os.path.isfile(f"{self.baits}.interval_list"):
                self.convert2intervals(self.baits, dict_file)

            # Run picard hsmetrics command
            hsmet_cmd = ["java", "-jar", "/usr/bin/picard.jar", "CollectHsMetrics", "-I", self.bam, "-O", f"{self.bam}.hsmetrics", "-R", self.reference, "-BAIT_INTERVALS", f"{self.baits}.interval_list", "-TARGET_INTERVALS", f"{self.bed}.interval_list"]
            self.system_p(hsmet_cmd)

            # Parse hsmetrics output file
            self.parse_hsmetrics(f"{self.bam}.hsmetrics")

        # Collect basic sequencing statistics
        LOG.info("Collecting basic stats...")
        sambamba_flagstat_cmd = f"sambamba flagstat {'-t '+ str(self.cpus) if self.cpus else ''} {self.bam}"
        flagstat = subprocess.check_output(sambamba_flagstat_cmd, shell=True, text=True).splitlines()
        num_reads = int(flagstat[0].split()[0])
        dup_reads = int(flagstat[3].split()[0])
        mapped_reads = int(flagstat[4].split()[0])

        # Get insert size metrics
        if self.paired:
            LOG.info("Collect insert sizes...")
            cmd = ["java", "-jar", "/usr/bin/picard.jar", "CollectInsertSizeMetrics", "-I", self.bam, "-O", f"{self.bam}.inssize", "-H", f"{self.bam}.ins.pdf", "-STOP_AFTER", "1000000"]
            self.system_p(cmd)

            # Parse ismetrics output file
            self.parse_ismetrics(f"{self.bam}.inssize")

            # Remove ismetrics files after parsing
            os.remove(f"{self.bam}.inssize")
            os.remove(f"{self.bam}.ins.pdf")

        out_prefix = f"{self.bam}_postalnQC"
        thresholds = ["1", "10", "30", "100", "250", "500", "1000"]

        # Index bam file if .bai does not exist
        if not os.path.exists(f"{self.bam}.bai"):
            LOG.info("Indexing bam file: %s.bai", self.bam)
            sambamba_index_cmd = ["sambamba", "index", self.bam]
            self.system_p(sambamba_index_cmd)

        # Generate sambamba depth command
        LOG.info("Collecting depth stats...")
        sambamba_depth_cmd = ["sambamba", "depth", "base", "-c", "0"]
        if self.cpus:
            sambamba_depth_cmd.extend(["-t", str(self.cpus)])
        if self.bed:
            sambamba_depth_cmd.extend(["-L", self.bed])
        sambamba_depth_cmd.extend([self.bam, "-o", f"{out_prefix}.basecov.bed"])
        self.system_p(sambamba_depth_cmd)

        # Parse base coverage file
        self.parse_basecov_bed(f"{out_prefix}.basecov.bed", thresholds)
        os.remove(f"{out_prefix}.basecov.bed")

        self.results['tot_reads'] = num_reads
        self.results['mapped_reads'] = mapped_reads
        self.results['dup_reads'] = dup_reads
        self.results['dup_pct'] = dup_reads / mapped_reads
        self.results['sample_id'] = self.sample_id

        return self.results


def parse_quast_results(file: File) -> QcMethodIndex:
    """Parse quast file and extract relevant metrics.

    Args:
        sep (str): seperator

    Returns:
        AssemblyQc: list of key-value pairs
    """
    LOG.info("Parsing tsv file: %s", file.name)
    creader = csv.reader(file, delimiter="\t")
    header = next(creader)
    raw = [dict(zip(header, row)) for row in creader]
    qc_res = QuastQcResult(
        total_length=int(raw[0]["Total length"]),
        reference_length=raw[0]["Reference length"],
        largest_contig=raw[0]["Largest contig"],
        n_contigs=raw[0]["# contigs"],
        n50=raw[0]["N50"],
        assembly_gc=raw[0]["GC (%)"],
        reference_gc=raw[0]["Reference GC (%)"],
        duplication_ratio=raw[0]["Duplication ratio"],
    )
    return QcMethodIndex(software=QcSoftware.QUAST, result=qc_res)


def parse_postalignqc_results(qc_dict: Dict[str, Any]) -> QcMethodIndex:
    """Parse postalignqc json file and extract relevant metrics.

    Args:
        sep (str): seperator

    Returns:
        PostAlignQc: list of key-value pairs
    """
    LOG.info("Parsing qc dict")
    qc_res = PostAlignQcResult(
        ins_size=int(float(qc_dict["ins_size"])),
        ins_size_dev=int(float(qc_dict["ins_size_dev"])),
        mean_cov=int(qc_dict["mean_cov"]),
        pct_above_x=qc_dict["pct_above_x"],
        mapped_reads=int(qc_dict["mapped_reads"]),
        tot_reads=int(qc_dict["tot_reads"]),
        iqr_median=float(qc_dict["iqr_median"]),
        quartile1=float(qc_dict["quartile1"]),
        median=float(qc_dict["median"]),
        quartile3=float(qc_dict["quartile3"]),
    )
    return QcMethodIndex(software=QcSoftware.POSTALIGNQC, result=qc_res)


def parse_alignment_results(sample_id: str, bam: str, reference: str, cpus: int, bed: str = None, baits: str = None) -> QcMethodIndex:
    """Parse bam file and extract relevant metrics.

    Returns:
        PostAlignQc: list of key-value pairs
    """
    LOG.info("Parsing bam file: %s", bam.name)
    qc = QC(sample_id, bam.name, reference.name, cpus, getattr(bed, 'name', None), getattr(baits, 'name', None))
    qc_dict = qc.run()
    qc_res = parse_postalignqc_results(qc_dict)
    return qc_res
