from torch.utils.data import Dataset, DataLoader
import sys
import os
import numpy as np
import pandas as pd
import h5py
from collections import Counter
import csv


class TCGAMeta(Dataset):
    """ Meta_TCGA Dataset.

    """

    def __init__(self, data_dir=None, dataset_transform=None, transform=None, target_transform=None, download=False, preload=True, min_samples_per_class=3, task_variables_file=None, gene_symbol_map_file=None):
        self.dataset_transform = dataset_transform
        self.target_transform = target_transform
        self.transform = transform
        self.gene_symbol_map_file = gene_symbol_map_file

        # specify a default data directory
        if data_dir is None:
            data_dir = os.path.join(os.path.dirname(__file__), 'data')

        if download:
            with open(os.path.join(os.path.dirname(__file__), 'cancers')) as f:
                cancers = f.readlines()
            # remove whitespace
            cancers = [x.strip() for x in cancers]

            _download(data_dir, cancers)
        
        self.task_ids = get_TCGA_task_ids(data_dir, min_samples_per_class, task_variables_file)

        if preload:
            try:
                hdf_file = os.path.join(data_dir, "TCGA_HiSeqV2.hdf5")
                f = h5py.File(hdf_file)
                self.gene_expression_data = f['dataset'][:]
                f.close()
                gene_ids_file = os.path.join(data_dir, 'gene_ids')
                all_sample_ids_file = os.path.join(data_dir, 'all_sample_ids')
                self.gene_ids = _read_string_list(gene_ids_file)
                self.all_sample_ids = _read_string_list(all_sample_ids_file)
                self.preloaded = (self.all_sample_ids, self.gene_ids, self.gene_expression_data)
            except:
                print('TCGA_HiSeqV2.hdf5 could not be read from the data_dir.')
                sys.exit()

        else:
            self.preloaded=None

    # convenience method to be used with torch dataloaders
    @staticmethod
    def collate_fn(data):
        """
        Args:
            task (Dataset) : A task from the TCGA Metadataset.

        Returns:
            dataset: the argument dataset unchanged.

            This function performs no operation. It is used to overwrite the default collate_fn of torchs
            DataLoader because it is not compatible with a batch of Datasets.
        """
        return data

    def get_dataloader(self, *args, **kwargs):
        """
        Args:
            *args : The conventional dataset arg which will be supressed
            **kwargs : The conventional kwargs of a torch Dataloader with exception of dataset and collate_fn

            Returns:
                Meta_TCGA_loader (DataLoader): a configured dataloader for the MetaTCGA dataset.

                A convenience function for creating a dataloader which handles passing the right collate_fn
                and the dataset.
        """

        # Delete those kwargs if the have been passed in error
        kwargs.pop('collate_fn', None)
        kwargs.pop('dataset', None)
        return DataLoader(self, **kwargs, collate_fn=TCGAMeta.collate_fn)

    def __getitem__(self, index):
        """
        Args:
            index (int): Index

        Returns:
            dataset: a dataset which represents a specific task from the set of TCGA tasks.

            A task is defined by a target variable, which should be predicted from the gene expression data of a patient.
            The target variable is a combination of a clinical attribute and one of 39 types of cancer.
            An example of a target variable is: 'gender-BRCA', where we predict gender for breast cancer(BRCA) patients.
        """
        dataset = TCGATask(self.task_ids[index], transform=self.transform, target_transform=self.target_transform, download=False, preloaded=self.preloaded, gene_symbol_map_file=self.gene_symbol_map_file)

        if self.dataset_transform is not None:
            dataset = self.dataset_transform(dataset)
        return dataset

    def __len__(self):
        return len(self.task_ids)


class TCGATask(Dataset):
    def __init__(self, task_id, data_dir=None, transform=None, target_transform=None, download=False, preloaded=None, gene_symbol_map_file=None):
        self.id = task_id
        self.transform = transform
        self.target_transform = target_transform

        task_variable, cancer = task_id

        # specify a default data directory
        if data_dir is None:
            data_dir = os.path.join(os.path.dirname(__file__), 'data')

        if download:
            _download(data_dir, [cancer])

        if preloaded is None:
            gene_ids_file = os.path.join(data_dir, 'gene_ids')
            all_sample_ids_file = os.path.join(data_dir, 'all_sample_ids')

            if not(os.path.isfile(gene_ids_file) and os.path.isfile(all_sample_ids_file)):
                raise ValueError('Preprocessed gene_ids and sample_ids list where not found in {}.'.format(data_dir))

            self.gene_ids = _read_string_list(gene_ids_file)
            self._all_sample_ids = _read_string_list(all_sample_ids_file)
        else:
            self._all_sample_ids, self.gene_ids, self._data = preloaded
            
        if gene_symbol_map_file:
            self.gene_ids = symbol_map(self.gene_ids, gene_symbol_map_file)

        # load the cancer specific matrix
        matrix = pd.read_csv(os.path.join(data_dir, 'clinicalMatrices', cancer + '_clinicalMatrix'), delimiter='\t')
        #matrix.drop_duplicates(subset=['sampleID'], keep='first', inplace=True)
        ids = matrix['sampleID']
        attribute = matrix[task_variable]

        # filter all elements where the clinical variable is not available or the associated gene expression data
        available_elements = attribute.notnull() & matrix['sampleID'].isin(self._all_sample_ids)
        sample_ids = ids[available_elements].tolist()
        filtered_attribute = attribute[available_elements].astype('category').cat
        self._labels = filtered_attribute.codes.tolist()
        self.categories = filtered_attribute.categories.tolist()
        self.num_classes = len(self.categories)

        # generator to retrieve the specific indices we need
        indices_to_load = [self._all_sample_ids.index(sample_id) for sample_id in sample_ids]
        indices_to_load, self._labels = zip(*sorted(zip(indices_to_load, self._labels)))

        # lazy loading or loading from preloaded data if available
        if preloaded is None:
            hdf_file = os.path.join(data_dir, "TCGA_HiSeqV2.hdf5")
            with h5py.File(hdf_file, 'r') as f:
                self._samples = f['dataset'][indices_to_load, :]
        else:
            self._samples = self._data[np.array(list(indices_to_load), dtype=int), :]
            
        self.input_size = self._samples.shape[1]

    def __getitem__(self, index):
        sample = self._samples[index, :]
        label = self._labels[index]

        if self.transform is not None:
            sample = self.transform(sample)

        if self.target_transform is not None:
            label = self.target_transform(label)

        return (sample, label)

    def __len__(self):
        return self._samples.shape[0]


def get_TCGA_task_ids(data_dir=None, min_samples_per_class=3, task_variables_file=None):
    # specify a default data directory
    if data_dir is None:
        data_dir = os.path.join(os.path.dirname(__file__), 'data')

    try:
        all_sample_ids_file = os.path.join(data_dir, 'all_sample_ids')
        all_sample_ids = _read_string_list(all_sample_ids_file)
    except:
        print('TCGA_HiSeqV2.hdf5 could not be read from the data_dir.')
        sys.exit()

    if task_variables_file is None:
        task_variables_file = os.path.join(os.path.dirname(__file__), 'task_variables')

    with open(task_variables_file) as f:
        task_variables = f.readlines()
    # remove whitespace
    task_variables = [x.strip() for x in task_variables]

    task_ids = []
    for filename in os.listdir(os.path.join(data_dir, 'clinicalMatrices')):
        matrix = pd.read_csv(os.path.join(data_dir, 'clinicalMatrices', filename), delimiter='\t')

        for task_variable in task_variables:
            try:
                # if this task_variable exists for this cancer find the sample_ids for this task
                filter_clinical_variable_present = matrix[task_variable].notnull()
                # filter out all sample_ids for which no valid value exists
                potential_sample_ids = matrix['sampleID'][filter_clinical_variable_present]
                # filter out all sample_ids for which no gene expression data exists
                task_sample_ids = set(potential_sample_ids).intersection(all_sample_ids)
#                task_sample_ids = [sample_id for sample_id in potential_sample_ids if sample_id in all_sample_ids]
            except KeyError:
                continue

            task_id = (task_variable, filename.split('_')[0])

            num_samples_per_label = Counter(matrix[task_variable][matrix['sampleID'].isin(task_sample_ids)])

            # only add this task for the specified range of number of samples
            num_samples_per_class_is_in_range = all([num_samples > min_samples_per_class for num_samples in num_samples_per_label.values()])
            # Make sure this task is not a one-class classification in the first place
            is_not_one_class = len(num_samples_per_label) > 1
            if num_samples_per_class_is_in_range and is_not_one_class:
                task_ids.append(task_id)
    return task_ids


def _download(data_dir, cancers):
    import academictorrents as at
    from six.moves import urllib
    import gzip

    # download files
    try:
        os.makedirs(os.path.join(data_dir, 'clinicalMatrices'))
    except OSError as e:
        if e.errno == 17:
            pass
        else:
            raise

    for cancer in cancers:
        filename = '{}_clinicalMatrix'.format(cancer)
        file_path = os.path.join(data_dir, 'clinicalMatrices', filename)
        decompressed_file_path = file_path.replace('.gz', '')

        if os.path.isfile(file_path):
            continue

        file_path += '.gz'

        url = 'https://tcga.xenahubs.net/download/TCGA.{}.sampleMap/{}_clinicalMatrix.gz'.format(cancer, cancer)

        print('Downloading ' + url)
        data = urllib.request.urlopen(url)

        with open(file_path, 'wb') as f:
            f.write(data.read())
        with open(decompressed_file_path, 'wb') as out_f, gzip.GzipFile(file_path) as zip_f:
            out_f.write(zip_f.read())
        os.unlink(file_path)

        if os.stat(decompressed_file_path).st_size == 0:
            os.remove(decompressed_file_path)
            error = IOError('Downloading {} from {} failed.'.format(filename, url))
            error.strerror = 'Downloading {} from {} failed.'.format(filename, url)
            error.errno = 5
            error.filename = decompressed_file_path
            raise error

    hdf_file = os.path.join(data_dir, "TCGA_HiSeqV2.hdf5")
    #csv_file = os.path.join(data_dir, 'HiSeqV2.gz')
    gene_ids_file = os.path.join(data_dir, 'gene_ids')
    all_sample_ids_file = os.path.join(data_dir, 'all_sample_ids')

    print('Downloading or checking for TCGA_HiSeqV2 using Academic Torrents')
    csv_file = at.get("e4081b995625f9fc599ad860138acf7b6eb1cf6f", datastore=data_dir)
    if not os.path.isfile(hdf_file) and os.path.isfile(csv_file):
        print("Downloaded to: " + csv_file)
        print("Converting TCGA CSV dataset to HDF5. This only happens on first run.")
        df = pd.read_csv(csv_file, compression="gzip", sep="\t")
        df = df.set_index('Sample')
        df = df.transpose()
        gene_ids = df.columns.values.tolist()
        all_sample_ids = df.index.values.tolist()
        with open(gene_ids_file, "w") as text_file:
            for gene_id in gene_ids:
                text_file.write('{}\n'.format(gene_id))
        with open(all_sample_ids_file, "w") as text_file:
            for sample_id in all_sample_ids:
                text_file.write('{}\n'.format(sample_id))

        f = h5py.File(hdf_file)
        f.create_dataset("dataset", data=df.values, compression="gzip")
        f.close()


def _read_string_list(path):
    with open(path) as f:
        string_list = f.readlines()
    # remove whitespace
    string_list = [x.strip() for x in string_list]
    return string_list

def symbol_map(gene_symbols, gene_symbol_map_file):
    # This gene code map was generated on February 18th, 2019
    # at this URL: https://www.genenames.org/cgi-bin/download/custom?col=gd_app_sym&col=gd_prev_sym&status=Approved&status=Entry%20Withdrawn&hgnc_dbtag=on&order_by=gd_app_sym_sort&format=text&submit=submit
    # it enables us to map the gene names to the newest version of the gene labels
    with open(gene_symbol_map_file) as csv_file:
        csv_reader = csv.reader(csv_file, delimiter='\t')
        line_count = 0
        x = {row[0]: row[1] for row in csv_reader}

        gene_symbol_map = {}
        for key, val in x.items():
            for v in val.split(", "):
                if key not in gene_symbols:
                    gene_symbol_map[v] = key
        
        
    return pd.Series(gene_symbols).replace(gene_symbol_map).values.tolist()