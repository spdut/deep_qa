from typing import Dict, List
from overrides import overrides

import numpy

class IndexedInstance:
    """
    An indexed data instance, which is a list of word indices, coupled with a label, and possibly
    an instance index.  An IndexedInstance is created from an Instance using a DataIndexer, and the
    indices here have no recoverable meaning without the DataIndexer.

    For example, we might have the following instance:
        Instance('Jamie is nice, Holly is mean', True, 25).
    After being converted into an IndexedInstance, we might have the following:
        IndexedInstance([1, 6, 7, 1, 6, 8], True, 25).
    This would mean that "Jamie" and "Holly" were OOV to the DataIndexer, and the other words were
    given indices.
    """
    def __init__(self, word_indices: List[int], label, index: int=None):
        """
        label and index have same values and meaning as in `Instance`.
        """
        self.word_indices = word_indices
        self.label = label
        self.index = index

    @classmethod
    def empty_instance(cls):
        return IndexedInstance([], None)

    def get_lengths(self) -> Dict[str, int]:
        """
        This simple IndexedInstance only has one padding dimension: word_indices.
        """
        return {'word_sequence_length': len(self.word_indices)}

    def pad(self, max_lengths: Dict[str, int]):
        """
        Pads (or truncates) self.word_indices to be of length max_lengths[0].  See comment on
        self.get_lengths() for why max_lengths is a list instead of an int.

        If we need to truncate self.word_indices, we do it from the _right_, not the left.  This is
        important for cases that are questions, with long set ups.  We at least want to get the
        question encoded, which is always at the end, even if we've lost much of the question set
        up.
        """
        desired_length = max_lengths['word_sequence_length']
        padded_word_indices = [0] * desired_length
        indices_length = min(len(self.word_indices), desired_length)
        if indices_length != 0:
            padded_word_indices[-indices_length:] = self.word_indices[-indices_length:]
        self.word_indices = padded_word_indices

    def as_training_data(self):
        word_array = numpy.asarray(self.word_indices, dtype='int32')
        label = numpy.zeros((2))
        if self.label is True:
            label[1] = 1
        elif self.label is False:
            label[0] = 1
        else:
            raise RuntimeError("Cannot make training data out of instances without labels!")
        return word_array, label


class IndexedLogicalFormInstance(IndexedInstance):
    """
    An IndexedLogicalFormInstance is a tree-structured instance, which represents a logical form
    like "for(depend_on(human, plant), oxygen)" as a pair of: (1) a (sequential) list of predicates
    and arguments, and (2) a list of shift/reduce operations, which allows recovery of the original
    tree structure from the sequential list of predicates and arguments.  This allows us to do tree
    composition in a compiled neural network - we just have to pad to the maximum transition
    length, and we can represent arbitrarily shaped trees.

    Idea taken from the SPINN paper by Sam Bowman and others (http://arxiv.org/pdf/1603.06021.pdf).
    """
    def __init__(self, word_indices: List[int], transitions: List[int], label: bool, index: int=None):
        super(IndexedLogicalFormInstance, self).__init__(word_indices, label, index)
        self.transitions = transitions

    @classmethod
    @overrides
    def empty_instance(cls):
        return IndexedLogicalFormInstance([], [], None)

    @overrides
    def get_lengths(self) -> Dict[str, int]:
        """
        Prep for padding; see comment on this method in the super class.  Here we extend the return
        value from our super class with the padding lengths necessary for `transitions`.
        """
        lengths = super(IndexedLogicalFormInstance, self).get_lengths()
        lengths['transition_length'] = len(self.transitions)
        return lengths

    @overrides
    def pad(self, max_lengths: Dict[str, int]):
        """
        We let the super class deal with padding word_indices; we'll worry about padding
        transitions.
        """
        super(IndexedLogicalFormInstance, self).pad(max_lengths)

        transition_length = max_lengths['transition_length']
        padded_transitions = [0] * transition_length
        indices_length = min(len(self.transitions), transition_length)
        if indices_length != 0:
            padded_transitions[-indices_length:] = self.transitions[-indices_length:]
        self.transitions = padded_transitions

    @overrides
    def as_training_data(self):
        word_array, label = super(IndexedLogicalFormInstance, self).as_training_data()
        transitions = numpy.asarray(self.transitions, dtype='int32')
        return (word_array, transitions), label


class IndexedBackgroundInstance(IndexedInstance):
    """
    An IndexedInstance that has background knowledge associated with it, where the background
    knowledge has also been indexed.
    """
    def __init__(self,
                 word_indices: List[int],
                 background_indices: List[List[int]],
                 label: bool,
                 index: int=None):
        super(IndexedBackgroundInstance, self).__init__(word_indices, label, index)
        self.background_indices = background_indices

    @classmethod
    @overrides
    def empty_instance(cls):
        return IndexedBackgroundInstance([], [], None)

    @overrides
    def get_lengths(self) -> Dict[str, int]:
        """
        Prep for padding; see comment on this method in the super class.  Here we extend the return
        value from our super class with the padding lengths necessary for background_indices.

        Additionally, as we currently use the same encoder for both a sentence and its background
        knowledge, we'll also modify the word_indices length to look at the background sentences
        too.
        """
        lengths = super(IndexedBackgroundInstance, self).get_lengths()
        lengths['background_sentences'] = len(self.background_indices)
        if self.background_indices:
            max_background_length = max(len(background) for background in self.background_indices)
            lengths['word_sequence_length'] = max(lengths['word_sequence_length'], max_background_length)
        return lengths

    @overrides
    def pad(self, max_lengths: Dict[str, int]):
        """
        We let the super class deal with padding word_indices; we'll worry about padding
        background_indices.  We need to pad it in two ways: (1) we need len(background_indices) to
        be the same for all instances, and (2) we need len(background_indices[i]) to be the same
        for all i, for all instances.  We'll use the word_indices length from the super class for
        (2).
        """
        super(IndexedBackgroundInstance, self).pad(max_lengths)
        background_length = max_lengths['background_sentences']
        word_sequence_length = max_lengths['word_sequence_length']

        # Padding (1): making sure we have the right number of background sentences.  We also need
        # to truncate, if necessary.
        if len(self.background_indices) > background_length:
            self.background_indices = self.background_indices[:background_length]
        for _ in range(background_length - len(self.background_indices)):
            self.background_indices.append([0])

        # Padding (2): making sure all background sentences have the right length.
        padded_background = []
        for background in self.background_indices:
            padded_word_indices = [0] * word_sequence_length
            indices_length = min(len(background), word_sequence_length)
            if indices_length != 0:
                padded_word_indices[-indices_length:] = background[-indices_length:]
            padded_background.append(padded_word_indices)
        self.background_indices = padded_background

    @overrides
    def as_training_data(self):
        word_array, label = super(IndexedBackgroundInstance, self).as_training_data()
        background_array = numpy.asarray(self.background_indices, dtype='int32')
        return (word_array, background_array), label


class IndexedQuestionInstance(IndexedInstance):
    """
    A QuestionInstance that has been indexed.  QuestionInstance has a better description of what
    this represents.
    """
    def __init__(self, options: List[IndexedInstance], label):
        self.options = options
        super(IndexedQuestionInstance, self).__init__([], label)

    @classmethod
    @overrides
    def empty_instance(cls):
        return IndexedQuestionInstance([], None)

    @overrides
    def get_lengths(self) -> Dict[str, int]:
        """
        Here we return the max of get_lengths on all of the Instances in self.options.
        """
        max_lengths = {}
        max_lengths['num_options'] = len(self.options)
        lengths = [instance.get_lengths() for instance in self.options]
        if not lengths:
            return max_lengths
        for key in lengths[0]:
            max_lengths[key] = max(x[key] for x in lengths)
        return max_lengths

    @overrides
    def pad(self, max_lengths: Dict[str, int]):
        """
        This method pads all of the underlying Instances in self.options.
        """
        num_options = max_lengths['num_options']

        # First we pad the number of options.
        while len(self.options) < num_options:
            self.options.append(self.options[0].empty_instance())
        self.options = self.options[:num_options]

        # Then we pad each option.
        for instance in self.options:  # type: IndexedInstance
            instance.pad(max_lengths)

    @overrides
    def as_training_data(self):
        inputs = []
        unzip_inputs = False
        for option in self.options:
            option_input, _ = option.as_training_data()
            if isinstance(option_input, tuple):
                unzip_inputs = True
            inputs.append(option_input)
        if unzip_inputs:
            inputs = tuple(zip(*inputs))  # pylint: disable=redefined-variable-type
        label = numpy.zeros(len(self.options))
        label[self.label] = 1
        return inputs, label