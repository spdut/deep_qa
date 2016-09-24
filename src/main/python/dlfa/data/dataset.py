import codecs
import logging
import random

from typing import Dict, List

from .instance import BackgroundTextInstance, Instance, IndexedInstance, TextInstance, QuestionInstance
from .tokenizer import tokenizers, Tokenizer
from .data_indexer import DataIndexer

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class Dataset:
    """
    A collection of Instances.

    This base class has general methods that apply to all collections of Instances.  That basically
    is just methods that operate on sets, like merging and truncating.
    """
    def __init__(self, instances: List[Instance]):
        """
        A Dataset just takes a list of instances in its constructor.  It's important that all
        subclasses have an identical constructor to this (though possibly with different Instance
        types).  If you change the constructor, you also have to override all methods in this base
        class that call the constructor, such as `merge()` and `truncate()`.
        """
        self.instances = instances

    def can_be_converted_to_questions(self):
        """
        This method checks that dataset matches the assumptions we make about question data: that
        it is a list of sentences corresponding to four-choice questions, with one correct answer
        for every four instances.

        So, specifically, we check that the number of instances is a multiple of four, and we check
        that each group of four instances has exactly one instance with label True, and all other
        labels are False (i.e., no None labels for validation data).
        """
        for instance in self.instances:
            if isinstance(instance, QuestionInstance):
                return False
        if len(self.instances) % 4 != 0:
            return False
        questions = zip(*[self.instances[i::4] for i in range(4)])
        for question in questions:
            question_labels = [instance.label for instance in question]
            label_counts = {x: question_labels.count(x) for x in set(question_labels)}
            if label_counts[True] != 1:
                return False
            if label_counts[False] != 3:
                return False
        return True

    def merge(self, other: 'Dataset') -> 'Dataset':
        """
        Combine two datasets.  If you call try to merge two Datasets of the same subtype, you will
        end up with a Dataset of the same type (i.e., calling IndexedDataset.merge() with another
        IndexedDataset will return an IndexedDataset).  If the types differ, this method currently
        raises an error, because the underlying Instance objects are not currently type compatible.
        """
        if type(self) is type(other):
            return self.__class__(self.instances + other.instances)
        else:
            raise RuntimeError("Cannot merge datasets with different types")

    def truncate(self, max_instances: int):
        """
        If there are more instances than `max_instances` in this dataset, returns a new dataset
        with a random subset of size `max_instances`.  If there are fewer than `max_instances`
        already, we just return self.
        """
        if len(self.instances) <= max_instances:
            return self
        new_instances = [i for i in self.instances]
        random.shuffle(new_instances)
        return self.__class__(new_instances[:max_instances])


class TextDataset(Dataset):
    """
    A Dataset of TextInstances, with a few helper methods.

    TextInstances aren't useful for much with Keras until they've been indexed.  So this class just
    has methods to read in data from a file and converting it into other kinds of Datasets.
    """
    def __init__(self, instances: List[TextInstance]):
        super(TextDataset, self).__init__(instances)

    def to_indexed_dataset(self, data_indexer: DataIndexer) -> 'IndexedDataset':
        '''
        Converts the Dataset into an IndexedDataset, given a DataIndexer.
        '''
        indexed_instances = [instance.to_indexed_instance(data_indexer) for instance in self.instances]
        return IndexedDataset(indexed_instances)

    def to_question_dataset(self) -> 'Dataset':
        assert self.can_be_converted_to_questions()
        questions = zip(*[self.instances[i::4] for i in range(4)])
        question_instances = []
        for question in questions:
            question_instances.append(QuestionInstance(question))
        return TextDataset(question_instances)

    @staticmethod
    def read_from_file(filename: str,
                       label: bool=None,
                       tokenizer: Tokenizer=tokenizers['default']()) -> 'TextDataset':
        lines = [x.strip() for x in codecs.open(filename, "r", "utf-8").readlines()]
        return TextDataset.read_from_lines(lines, label, tokenizer)

    @staticmethod
    def read_from_lines(lines: List[str],
                        label: bool=None,
                        tokenizer: Tokenizer=tokenizers['default']()) -> 'TextDataset':
        instances = [TextInstance.read_from_line(x, label, tokenizer) for x in lines]
        num_positive = len([x for x in instances if x.label is True])
        num_negative = len([x for x in instances if x.label is False])
        num_unknown = len([x for x in instances if x.label is None])
        logger.info("Finished reading dataset; there are %d positives, %d negatives, and %d unknown",
                    num_positive, num_negative, num_unknown)
        return TextDataset(instances)

    @staticmethod
    def read_background_from_file(dataset: 'TextDataset',
                                  filename: str,
                                  tokenizer: Tokenizer=tokenizers['default']()) -> 'TextDataset':
        """
        Reads a file formatted as background information and matches the background to the
        sentences in the given dataset.  The given dataset must have instance indices, so we can
        match the background information in the file to the instances in the dataset.

        The format for the file is assumed to be the following:
        [sentence index][tab][background 1][tab][background 2][tab][...]
        where [sentence index] corresponds to the index of one of the instances in `dataset`.

        This code will also work if the data is formatted simply as [index][tab][sentence], one per
        line.
        """
        new_instances = {}
        for instance in dataset.instances:
            background_instance = BackgroundTextInstance(instance.text,
                                                         [],
                                                         instance.label,
                                                         instance.index,
                                                         tokenizer)
            new_instances[instance.index] = background_instance
        for line in codecs.open(filename, "r", "utf-8"):
            fields = line.strip().split("\t")
            index = int(fields[0])
            if index in new_instances:
                instance = new_instances[index]
                for sequence in fields[1:]:
                    instance.background.append(sequence)
        return TextDataset(list(new_instances.values()))


class IndexedDataset(Dataset):
    """
    A Dataset of IndexedInstances, with some helper methods.

    IndexedInstances have text sequences replaced with lists of word indices, and are thus able to
    be padded to consistent lengths and converted to training inputs.
    """
    def __init__(self, instances: List[IndexedInstance]):
        super(IndexedDataset, self).__init__(instances)

    def max_lengths(self):
        max_lengths = {}
        lengths = [instance.get_lengths() for instance in self.instances]
        if not lengths:
            return max_lengths
        for key in lengths[0]:
            max_lengths[key] = max(x[key] for x in lengths)
        return max_lengths

    def pad_instances(self, max_lengths: Dict[str, int]):
        """
        Make all of the IndexedInstances in the dataset have the same length by padding them (in
        the front) with zeros.

        If max_length is given for a particular dimension, we will pad all instances to that length
        (including left-truncating instances if necessary).  If not, we will find the longest
        instance and pad all instances to that length.  Note that max_lengths is a _List_, not an
        int - there could be several dimensions on which we need to pad, depending on what kind of
        instance we are dealing with.

        This method _modifies_ the current object, it does not return a new IndexedDataset.
        """
        # First we need to decide _how much_ to pad.  To do that, we find the max length for all
        # relevant padding decisions from the instances themselves.  Then we check whether we were
        # given a max length for a particular dimension.  If we were, we use that instead of the
        # instance-based one.
        logger.info("Getting max lengths from instances")
        instance_max_lengths = self.max_lengths()
        logger.info("Instance max lengths: %s", str(instance_max_lengths))
        lengths_to_use = {}
        for key in instance_max_lengths:
            if max_lengths[key] is not None:
                lengths_to_use[key] = max_lengths[key]
            else:
                lengths_to_use[key] = instance_max_lengths[key]

        logger.info("Now actually padding instances to length: %s", str(lengths_to_use))
        for instance in self.instances:
            instance.pad(lengths_to_use)

    def as_training_data(self, shuffle=True):
        """
        Takes each IndexedInstance and converts it into (inputs, labels), according to the
        Instance's as_training_data() method.  Note that you might need to call numpy.asarray() on
        the results of this; we don't do that for you, because the inputs might be complicated.
        """
        inputs = []
        labels = []
        instances = self.instances
        if shuffle:
            random.shuffle(instances)
        for instance in instances:
            instance_inputs, label = instance.as_training_data()
            inputs.append(instance_inputs)
            labels.append(label)
        return inputs, labels