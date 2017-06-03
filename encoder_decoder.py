import tensorflow as tf
from input_utils import InputPipeline, EmbeddingHandler
import copy
from os import getcwd
from os.path import join
import timeit




class EncoderDecoderReconstruction:
    def __init__(self, vocabulary_size, embedding_size, hidden_states, learning_rate=0.00001,
                 cross_entropy_weight=0.001, reconstruction_weight=1.0):
        self.should_print = tf.placeholder_with_default(False, shape=())
        # all inputs should have a <START> token before every sentence
        self.inputs = tf.placeholder(tf.int32, (None, None))  # (batch, time, in)
        inputs = self.print_tensor_with_shape(self.inputs, "inputs")

        # embedding
        w = tf.Variable(tf.random_normal(shape=[vocabulary_size, embedding_size]),
                        trainable=False, name="WordVectors")
        self.embedding_placeholder = tf.placeholder(tf.float32, [vocabulary_size, embedding_size])
        # you can call assign embedding op to init the embedding
        self.assign_embedding = tf.assign(w, self.embedding_placeholder)
        self.embedding = tf.nn.embedding_lookup(w, inputs)
        embedding = self.print_tensor_with_shape(self.embedding, "embedding")

        # holds all the relevant sizes of the output sizes for the RNNs
        layers_sizes = copy.deepcopy(hidden_states)
        layers_sizes.insert(0, embedding_size)

        # important sizes
        batch_size = tf.shape(inputs)[0]
        sentence_length = tf.shape(inputs)[1]

        # encoder
        with tf.variable_scope('encoder'):
            encoder_cells = []
            for hidden_size in layers_sizes[1:]:
                encoder_cells.append(tf.contrib.rnn.BasicLSTMCell(hidden_size, state_is_tuple=True))

            multilayer_encoder = tf.contrib.rnn.MultiRNNCell(encoder_cells)
            initial_state = multilayer_encoder.zero_state(batch_size, tf.float32)
            # for the encoder input we need to skip <START> token.
            encoder_input = embedding[:, 1:, :]
            rnn_outputs, _ = tf.nn.dynamic_rnn(multilayer_encoder, encoder_input,
                                               initial_state=initial_state,
                                               time_major=False)
        # the last output of the encoder provides the hidden vector
        self.encoded_vector = rnn_outputs[:, -1, :]
        encoded_vector = self.print_tensor_with_shape(self.encoded_vector, "encoded_vector")

        # for the decoder input need to append encoded vector to embedding of each input
        decoder_inputs = tf.expand_dims(encoded_vector, 1)
        decoder_inputs = tf.tile(decoder_inputs, [1, sentence_length-1, 1])
        # don't take the entire input: ignore the last element in inputs and do include the <START> token.
        decoder_inputs = tf.concat((embedding[:, :-1, :], decoder_inputs), axis=2)
        decoder_inputs = self.print_tensor_with_shape(decoder_inputs, "decoder_inputs")

        # decoder
        with tf.variable_scope('decoder'):
            decoder_cells = []
            layers_sizes.reverse() # the layers are mirror image of the encoder
            for hidden_size in layers_sizes[1:]:
                decoder_cells.append(tf.contrib.rnn.BasicLSTMCell(hidden_size, state_is_tuple=True))
            multilayer_decoder = tf.contrib.rnn.MultiRNNCell(decoder_cells)
            initial_state = multilayer_decoder.zero_state(batch_size, tf.float32)
            rnn_outputs, _ = tf.nn.dynamic_rnn(multilayer_decoder, decoder_inputs,
                                               initial_state=initial_state,
                                               time_major=False)
        self.decoded_vector = rnn_outputs
        decoded_vector = self.print_tensor_with_shape(self.decoded_vector, "decoded_vector")

        # compute loss
        loss_embedding = tf.reduce_mean(tf.squared_difference(embedding[:, 1:, :], decoded_vector))
        self.loss = reconstruction_weight * loss_embedding

        if cross_entropy_weight > 0.0:
            # linear layer to project to vocabulary
            decoder_reshaped = tf.reshape(decoded_vector, (batch_size * (sentence_length-1), embedding_size))
            vocab_logits = tf.contrib.layers.linear(decoder_reshaped, vocabulary_size)
            inputs_reshaped = tf.reshape(inputs[:, 1:], (batch_size * (sentence_length-1), ))

            cross_entropy_loss = tf.reduce_mean(
                tf.nn.sparse_softmax_cross_entropy_with_logits(labels=inputs_reshaped, logits=vocab_logits))
            self.loss += cross_entropy_weight * cross_entropy_loss

        # train step
        self.train = tf.train.AdamOptimizer(learning_rate=learning_rate).minimize(self.loss)

    def print_tensor_with_shape(self, tensor, name):
        return tf.cond(self.should_print,
                       lambda: tf.Print(
                           tf.Print(tensor, [tensor], message=name + ":"),
                           [tf.shape(tensor)], message=name + " shape:"),
                       lambda: tf.identity(tensor))


glove_file = join(getcwd(), "data", "glove.6B", "glove.6B.50d.txt")
yoda_file = join(getcwd(), "yoda", "english_yoda.text")

# create input pipeline
embedding_handler = EmbeddingHandler(pretrained_glove_file=glove_file,
                                     force_vocab=False, start_of_sentence_token='START', unknown_token='UNK')
input_stream = InputPipeline(text_file=yoda_file, embedding_handler=embedding_handler, limit_sentences=10)

model = EncoderDecoderReconstruction(embedding_handler.vocab_len, embedding_handler.embedding_size,
                                     hidden_states=[10, 5], cross_entropy_weight=1.0, reconstruction_weight=1.0,
                                     learning_rate=0.001)
session = tf.Session()
# For some reason it is our job to do this:
session.run(tf.global_variables_initializer())

# init embedding
_ = session.run(model.assign_embedding, {model.embedding_placeholder: embedding_handler.embedding_np})

for epoch in range(100):
    start_time = timeit.default_timer()
    epoch_loss = 0
    num_sentences = 0
    data_iter = input_stream.batch_iterator(shuffle=True, maximal_batch=2)
    for batch, indexed_batch in data_iter:
        num_sentences += len(batch)
        epoch_loss += session.run([model.loss, model.train], {
            model.inputs: indexed_batch,
        })[0]
    epoch_loss /= num_sentences
    elapsed = timeit.default_timer() - start_time
    print("Epoch %d, elapsed time: %.2f train error: %.2f" % (epoch, elapsed, epoch_loss))

    # TODO: add validation error + hidden vector of the same sentence
    # validation_error = 0
    # data_iter = test_pipe.batch_iterator(1, shuffle=True)
    # for i, (x, y) in enumerate(data_iter):
    #     validation_error += session.run(lstm_model.accuracy, {
    #         lstm_model.inputs:  x,
    #         lstm_model.outputs: y,
    #     })
    # validation_error /= (i+1)
    # print("Epoch %d, train error: %.2f, valid accuracy: %.1f %%" % (epoch, epoch_error, validation_error * 100.0))





