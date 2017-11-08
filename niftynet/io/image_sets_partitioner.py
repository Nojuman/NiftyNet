# -*- coding: utf-8 -*-
"""
This module manages a table of subject ids and
their associated image file names.
A subset of the table can be retrieved by partitioning the set of images into
subsets of `train`, `validation`, `inference`.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import os
import random

import pandas
import tensorflow as tf  # to use the system level logging

from niftynet.utilities.decorators import singleton
from niftynet.utilities.filename_matching import KeywordsMatching
from niftynet.utilities.niftynet_global_config import NiftyNetGlobalConfig
from niftynet.utilities.util_common import look_up_operations
from niftynet.utilities.util_csv import match_and_write_filenames_to_csv
from niftynet.utilities.util_csv import write_csv

COLUMN_UNIQ_ID = 'subject_id'
COLUMN_PHASE = 'phase'
TRAIN = 'train'
VALID = 'validation'
INFER = 'inference'
ALL = 'all'
SUPPORTED_PHASES = {TRAIN, VALID, INFER, ALL}


@singleton
class ImageSetsPartitioner(object):
    """
    This class maintains a pandas.dataframe of filenames for all input sections.
    The list of filenames are obtained by searching the specified folders
    or loading from an existing csv file.

    Users can query a subset of the dataframe by train/valid/infer partition
    label and input section names.
    """

    # dataframe (table) of file names in a shape of subject x modality
    _file_list = None
    # dataframes of subject_id:phase_id
    _partition_ids = None

    data_param = None
    ratios = None
    new_partition = False

    # for saving the splitting index
    data_split_file = ""
    # default parent folder location for searching the image files
    default_image_file_location = \
        NiftyNetGlobalConfig().get_niftynet_home_folder()

    def initialise(self,
                   data_param,
                   new_partition=False,
                   data_split_file="./test.csv",
                   ratios=None):
        """
        Set the data partitioner parameters
        data_param: corresponding to all config sections
        new_partition: bool value indicating whether to generate new
                       partition ids and overwrite csv file
        data_split_file: location of the partition id file
        ratios: a tuple/list with two elements:
               (fraction of the validation set,
                fraction of the inference set)
               initialise to None will disable data partitioning
               and get_file_list always returns all subjects.
        """
        self.data_param = data_param
        self.data_split_file = data_split_file
        self.ratios = ratios

        self._file_list = None
        self._partition_ids = None

        self.load_data_sections_by_subject()
        self.new_partition = new_partition
        self.randomly_split_dataset(overwrite=new_partition)
        tf.logging.info(self)

    def number_of_subjects(self, phase=ALL):
        """
        query number of images according to phase
        :param phase:
        :return:
        """
        if self._file_list is None:
            return 0
        phase = look_up_operations(phase.lower(), SUPPORTED_PHASES)
        if phase == ALL or self._partition_ids is None:
            return self._file_list[COLUMN_UNIQ_ID].count()
        selector = self._partition_ids[COLUMN_PHASE] == phase
        return self._partition_ids[selector].count()[COLUMN_UNIQ_ID]

    def get_file_list(self, phase=ALL, section_names=None):
        """
        Returns a pandas.dataframe of file names
        """
        if self._file_list is None:
            tf.logging.fatal('Empty file list, please initialise'
                             'ImageSetsPartitioner first.')
            raise RuntimeError
        if section_names:
            look_up_operations(section_names, set(self._file_list))
        if phase == ALL:
            self._file_list = self._file_list.sort_index()
            if section_names:
                return self._file_list[section_names]
            return self._file_list
        if self._partition_ids is None:
            tf.logging.fatal('No partition ids available.')
            if self.new_partition:
                tf.logging.fatal('Unable to create new partitions,'
                                 'splitting ratios: %s, writing file %s',
                                 self.ratios, self.data_split_file)
            elif os.path.isfile(self.data_split_file):
                tf.logging.fatal(
                    'Unable to load %s, initialise the'
                    'ImageSetsPartitioner with `new_partition=True`'
                    'to overwrite the file.',
                    self.data_split_file)
            raise ValueError

        selector = self._partition_ids[COLUMN_PHASE] == phase
        selected = self._partition_ids[selector][[COLUMN_UNIQ_ID]]
        if selected.empty:
            tf.logging.warning(
                'Empty subset for phase [%s], returning None as file list. '
                'Please adjust splitting fractions.', phase)
            return None
        subset = pandas.merge(self._file_list, selected, on=COLUMN_UNIQ_ID)
        if section_names:
            return subset[section_names]
        return subset

    def load_data_sections_by_subject(self):
        """
        Go through all input data sections, converting each section
        to a list of file names.  These lists are merged on COLUMN_UNIQ_ID

        This function sets self._file_list
        """
        if not self.data_param:
            tf.logging.fatal(
                'Nothing to load, please check input sections in the config.')
            raise ValueError
        self._file_list = None
        for section_name in self.data_param:
            modality_file_list = self.grep_files_by_data_section(section_name)
            if self._file_list is None:
                # adding all rows of the first modality
                self._file_list = modality_file_list
                continue
            n_rows = self._file_list[COLUMN_UNIQ_ID].count()
            self._file_list = pandas.merge(self._file_list,
                                           modality_file_list,
                                           on=COLUMN_UNIQ_ID)
            if self._file_list[COLUMN_UNIQ_ID].count() < n_rows:
                tf.logging.warning('rows not matched in section [%s]',
                                   section_name)

        if self._file_list is None or self._file_list.size == 0:
            tf.logging.fatal(
                "empty filename lists, please check the csv "
                "files. (removing csv_file keyword if it is in the config file "
                "to automatically search folders and generate new csv "
                "files again)\n\n"
                "Please note in the matched file names, each subject id are "
                "created by removing all keywords listed `filename_contains` "
                "in the config.\n\n"
                "E.g., `filename_contains=foo, bar` will match file "
                "foo_subject42_bar.nii.gz, and the subject id is _subject42_.")
            raise IOError

    def grep_files_by_data_section(self, modality_name):
        """
        list all files by a given input data section,
        if the `csv_file` property of the section corresponds to a file,
            read the list from the file;
        otherwise
            write the list to `csv_file`.

        returns: a table with two columns,
                 the column names are (COLUMN_UNIQ_ID, modality_name)
        """
        if modality_name not in self.data_param:
            tf.logging.fatal('unknown section name [%s], '
                             'current input section names: %s.',
                             modality_name, list(self.data_param))
            raise ValueError

        # input data section must have a `csv_file` section for loading
        # or writing filename lists
        try:
            csv_file = self.data_param[modality_name].csv_file
        except AttributeError:
            tf.logging.fatal('Missing `csv_file` field in the config file, '
                             'unknown configuration format.')
            raise

        if hasattr(self.data_param[modality_name], 'path_to_search') and \
                self.data_param[modality_name].path_to_search:
            tf.logging.info('[%s] search file folders, writing csv file %s',
                            modality_name, csv_file)
            section_properties = self.data_param[modality_name].__dict__.items()
            # grep files by section properties and write csv
            matcher = KeywordsMatching.from_tuple(
                section_properties,
                self.default_image_file_location)
            match_and_write_filenames_to_csv([matcher], csv_file)
        else:
            tf.logging.info(
                '[%s] using existing csv file %s, skipped filenames search',
                modality_name, csv_file)

        if not os.path.isfile(csv_file):
            tf.logging.fatal(
                '[%s] csv file %s not found.', modality_name, csv_file)
            raise IOError
        try:
            csv_list = pandas.read_csv(
                csv_file,
                header=None,
                dtype=(str, str),
                names=[COLUMN_UNIQ_ID, modality_name])
        except Exception as csv_error:
            tf.logging.fatal(repr(csv_error))
            raise
        return csv_list

    def randomly_split_dataset(self, overwrite=False):
        """
        Label each subject as one of the 'TRAIN', 'VALID', 'INFER',
        use self.ratios to compute the size of each set.
        the results will be written to self.data_split_file if overwrite
        otherwise it trys to read partition labels from it.

        This function sets self._partition_ids
        """
        if overwrite:
            try:
                valid_fraction, infer_fraction = self.ratios
                valid_fraction = max(min(1.0, float(valid_fraction)), 0.0)
                infer_fraction = max(min(1.0, float(infer_fraction)), 0.0)
            except (TypeError, ValueError):
                tf.logging.fatal(
                    'Unknown format of faction values %s', self.ratios)
                raise
            n_total = self.number_of_subjects()
            n_valid = int(math.ceil(n_total * valid_fraction))
            n_infer = int(math.ceil(n_total * infer_fraction))
            n_train = int(n_total - n_infer - n_valid)
            phases = [TRAIN] * n_train + \
                     [VALID] * n_valid + \
                     [INFER] * n_infer
            if len(phases) > n_total:
                phases = phases[:n_total]
            random.shuffle(phases)
            write_csv(self.data_split_file,
                      zip(self._file_list[COLUMN_UNIQ_ID], phases))
        else:
            if self.ratios:
                tf.logging.warning('Loading from existing partitioning file,'
                                   'ignoring partitioning ratios.')

        if os.path.isfile(self.data_split_file):
            try:
                self._partition_ids = pandas.read_csv(
                    self.data_split_file,
                    header=None,
                    dtype=(str, str),
                    names=[COLUMN_UNIQ_ID, COLUMN_PHASE])
            except Exception as csv_error:
                tf.logging.warning(repr(csv_error))

    def __str__(self):
        return self.to_string()

    def to_string(self):
        """
        Print summary of the partitioner
        """
        summary_str = '\nNumber of subjects {}, '.format(
            self.number_of_subjects())
        if self._file_list is not None:
            summary_str += 'input section names: {}\n'.format(
                list(self._file_list))
        if self.ratios:
            summary_str += \
                'data partitioning (percentage):\n' \
                '-- {} {} ({}),\n' \
                '-- {} {} ({}),\n' \
                '-- {} {}.\n'.format(
                    VALID, self.number_of_subjects(VALID), self.ratios[0],
                    INFER, self.number_of_subjects(INFER), self.ratios[1],
                    TRAIN, self.number_of_subjects(TRAIN))
        else:
            summary_str += '-- using all subjects ' \
                           '(without data partitioning).\n'
        return summary_str
