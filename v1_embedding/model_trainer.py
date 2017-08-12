import tensorflow as tf
import time
import yaml
from datasets.multi_batch_iterator import MultiBatchIterator
from datasets.yelp_helpers import YelpSentences
from v1_embedding.convergence_policy import ConvergencePolicy
from v1_embedding.embedding_translator import EmbeddingTranslator
from v1_embedding.embedding_encoder import EmbeddingEncoder
from v1_embedding.embedding_decoder import EmbeddingDecoder
from v1_embedding.embedding_discriminator import EmbeddingDiscriminator
from v1_embedding.loss_handler import LossHandler
from v1_embedding.model_trainer_base import ModelTrainerBase
from v1_embedding.word_indexing_embedding_handler import WordIndexingEmbeddingHandler


# this model tries to transfer from one domain to another.
# 1. the encoder doesn't know the domain it is working on
# 2. target are encoded and decoded (to target) then cross entropy loss is applied between the origin and the result
# 3. source is encoded decoded to target and encoded again, then L2 loss is applied between the context vectors.
# 4. an adversarial component is trained to distinguish true target from transferred targets using professor forcing
class ModelTrainer(ModelTrainerBase):
    def __init__(self, config_file, operational_config_file):
        ModelTrainerBase.__init__(self, config_file=config_file, operational_config_file=operational_config_file)

        # placeholders for dropouts
        self.dropout_placeholder = tf.placeholder(tf.float32, shape=(), name='dropout_placeholder')
        self.discriminator_dropout_placeholder = tf.placeholder(tf.float32, shape=(),
                                                                name='discriminator_dropout_placeholder')
        # placeholder for source sentences (batch, time)=> index of word s.t the padding is on the left
        self.left_padded_source_batch = tf.placeholder(tf.int64, shape=(None, None), name='left_padded_source_batch')
        # placeholder for source sentences (batch, time)=> index of word s.t the padding is on the right
        self.right_padded_source_batch = tf.placeholder(tf.int64, shape=(None, None), name='right_padded_source_batch')
        # placeholder for target sentences (batch, time)=> index of word s.t the padding is on the left
        self.left_padded_target_batch = tf.placeholder(tf.int64, shape=(None, None), name='left_padded_target_batch')
        # placeholder for target sentences (batch, time)=> index of word s.t the padding is on the right
        self.right_padded_target_batch = tf.placeholder(tf.int64, shape=(None, None), name='right_padded_target_batch')

        self.dataset_neg = YelpSentences(positive=False, limit_sentences=self.config['sentence']['limit'],
                                         dataset_cache_dir=self.dataset_cache_dir, dataset_name='neg')
        self.dataset_pos = YelpSentences(positive=True, limit_sentences=self.config['sentence']['limit'],
                                         dataset_cache_dir=self.dataset_cache_dir, dataset_name='pos')
        datasets = [self.dataset_neg, self.dataset_pos]
        self.embedding_handler = WordIndexingEmbeddingHandler(
            self.embedding_dir,
            datasets,
            self.config['embedding']['word_size'],
            self.config['embedding']['min_word_occurrences']
        )
        self.embedding_translator = EmbeddingTranslator(self.embedding_handler,
                                                        self.config['model']['translation_hidden_size'],
                                                        self.config['embedding']['should_train'],
                                                        self.dropout_placeholder)
        self.encoder = EmbeddingEncoder(self.config['model']['encoder_hidden_states'],
                                        self.dropout_placeholder,
                                        self.config['model']['bidirectional_encoder'])
        self.decoder = EmbeddingDecoder(self.embedding_handler.get_embedding_size(),
                                        self.config['model']['decoder_hidden_states'],
                                        self.dropout_placeholder,
                                        self.config['sentence']['max_length'])
        self.discriminator = EmbeddingDiscriminator(self.config['model']['discriminator_hidden_states'],
                                                    self.config['model']['discriminator_dense_hidden_size'],
                                                    self.discriminator_dropout_placeholder,
                                                    self.config['model']['bidirectional_discriminator'])
        self.loss_handler = LossHandler(self.embedding_handler.get_vocabulary_length())

        # losses:
        self.discriminator_step_prediction, self.discriminator_loss, self.discriminator_accuracy_for_discriminator = \
            self.get_discriminator_loss(
                self.left_padded_source_batch,
                self.left_padded_target_batch,
                self.right_padded_target_batch
            )

        self.generator_step_prediction, self.generator_loss, self.discriminator_accuracy_for_generator, \
        self.dicriminator_loss_on_generator_step = \
            self.get_generator_loss(
                self.left_padded_source_batch,
                self.left_padded_target_batch,
                self.right_padded_target_batch
            )

        # train steps
        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        with tf.variable_scope('TrainSteps'):
            with tf.variable_scope('TrainDiscriminatorSteps'):
                discriminator_optimizer = tf.train.GradientDescentOptimizer(self.config['model']['learn_rate'])
                discriminator_var_list = self.discriminator.get_trainable_parameters()
                discriminator_grads_and_vars = discriminator_optimizer.compute_gradients(
                    self.discriminator_loss,
                    colocate_gradients_with_ops=True,
                    var_list=discriminator_var_list
                )
                with tf.control_dependencies(update_ops):
                    self.discriminator_train_step = discriminator_optimizer.apply_gradients(discriminator_grads_and_vars)
            with tf.variable_scope('TrainGeneratorSteps'):
                generator_optimizer = tf.train.GradientDescentOptimizer(self.config['model']['learn_rate'])
                generator_var_list = self.encoder.get_trainable_parameters() + \
                                     self.decoder.get_trainable_parameters() + \
                                     self.embedding_translator.get_trainable_parameters()
                generator_grads_and_vars = generator_optimizer.compute_gradients(
                    self.generator_loss,
                    colocate_gradients_with_ops=True,
                    var_list=generator_var_list
                )
                with tf.control_dependencies(update_ops):
                    self.generator_train_step = generator_optimizer.apply_gradients(generator_grads_and_vars)

        # do transfer
        with tf.variable_scope('TransferSourceToTarget'):
            transferred_embeddings = self._transfer(self.left_padded_source_batch)
            transferred_logits = self.embedding_translator.translate_embedding_to_vocabulary_logits(transferred_embeddings)
            self.transfer = self.embedding_translator.translate_logits_to_words(transferred_logits)

        # iterators
        self.batch_iterator = MultiBatchIterator(datasets,
                                                 self.embedding_handler,
                                                 self.config['sentence']['min_length'],
                                                 self.config['model']['batch_size'])

        # iterators
        self.batch_iterator_validation = MultiBatchIterator(datasets,
                                                            self.embedding_handler,
                                                            self.config['sentence']['min_length'],
                                                            2)
        # train loop parameters:
        self.policy = ConvergencePolicy()

    def _encode(self, left_padded_input):
        embedding = self.embedding_translator.embed_inputs(left_padded_input)
        return self.encoder.encode_inputs_to_vector(embedding, domain_identifier=None)

    def _transfer(self, left_padded_source):
        encoded_source = self._encode(left_padded_source)
        return self.decoder.do_iterative_decoding(encoded_source, domain_identifier=None)

    def _teacher_force_target(self, left_padded_target_batch, right_padded_target_batch):
        encoded_target = self._encode(left_padded_target_batch)
        right_padded_target_embedding = self.embedding_translator.embed_inputs(right_padded_target_batch)
        return self.decoder.do_teacher_forcing(encoded_target,
                                               right_padded_target_embedding[:, :-1, :],
                                               domain_identifier=None)

    def _get_discriminator_prediction_loss_and_accuracy(self, transferred_source, teacher_forced_target):
        sentence_length = tf.shape(teacher_forced_target)[1]
        transferred_source_normalized = transferred_source[:, :sentence_length, :]
        prediction = self.discriminator.predict(tf.concat((transferred_source_normalized, teacher_forced_target),
                                                          axis=0))
        transferred_batch_size = tf.shape(transferred_source)[0]

        prediction_transferred = prediction[:transferred_batch_size, :]
        prediction_target = prediction[transferred_batch_size:, :]
        total_loss, total_accuracy = self.loss_handler.get_discriminator_loss(prediction_transferred, prediction_target)

        return prediction, total_loss, total_accuracy

    def get_discriminator_loss(self, left_padded_source_batch, left_padded_target_batch, right_padded_target_batch):
        # calculate the source-encoded-as-target loss
        sentence_length = tf.shape(left_padded_source_batch)[1]
        transferred_source = self._transfer(left_padded_source_batch)[:, :sentence_length, :]

        # calculate the teacher forced loss
        teacher_forced_target = self._teacher_force_target(left_padded_target_batch, right_padded_target_batch)

        return self._get_discriminator_prediction_loss_and_accuracy(transferred_source, teacher_forced_target)

    def get_generator_loss(self, left_padded_source_batch, left_padded_target_batch, right_padded_target_batch):
        encoded_source = self._encode(left_padded_source_batch)

        # reconstruction loss - recover target
        teacher_forced_target = self._teacher_force_target(left_padded_target_batch, right_padded_target_batch)
        reconstructed_target_logits = self.embedding_translator.translate_embedding_to_vocabulary_logits(
            teacher_forced_target)
        reconstruction_loss = self.loss_handler.get_sentence_reconstruction_loss(right_padded_target_batch,
                                                                                 reconstructed_target_logits)

        # semantic vector distance
        transferred_source = self.decoder.do_iterative_decoding(encoded_source, domain_identifier=None)
        encoded_again = self.encoder.encode_inputs_to_vector(transferred_source, domain_identifier=None)
        semantic_distance_loss = self.loss_handler.get_context_vector_distance_loss(encoded_source, encoded_again)

        # professor forcing loss source
        discriminator_prediction, discriminator_loss, discriminator_accuracy = \
            self._get_discriminator_prediction_loss_and_accuracy(
                transferred_source, teacher_forced_target
            )

        total_loss = self.config['model']['reconstruction_coefficient'] * reconstruction_loss \
                     + self.config['model']['semantic_distance_coefficient'] * semantic_distance_loss \
                     - discriminator_loss
        return discriminator_prediction, total_loss, discriminator_accuracy, discriminator_loss

    def do_generator_train(self, sess, global_step, epoch_num, batch_index, feed_dictionary):
        # TODO: outputs to measure progress, summaries
        print('started generator')
        print('running loss: {}'.format(self.policy.running_loss))  # TODO: remove
        execution_list = [
            self.generator_step_prediction,
            self.dicriminator_loss_on_generator_step,
            self.discriminator_accuracy_for_generator
        ]
        pred, loss, acc = sess.run(execution_list, feed_dictionary)
        print('pred: {}'.format(pred))
        print('acc: {}'.format(acc))
        print('loss: {}'.format(loss))
        if self.policy.should_train_generator(global_step, epoch_num, batch_index, pred, loss, acc):
            # the generator is still improving
            print('new running loss: {}'.format(self.policy.running_loss))  # TODO: remove
            print()
            sess.run(self.generator_train_step, feed_dictionary)
        else:
            print('generator too good - training discriminator')
            print()
            # the generator is no longer improving, will train discriminator next
            self.policy.do_train_switch(start_training_generator=False)
            self.do_discriminator_train(sess, global_step, epoch_num, batch_index, feed_dictionary)

    def do_discriminator_train(self, sess, global_step, epoch_num, batch_index, feed_dictionary):
        # TODO: outputs to measure progress, summaries
        print('started discriminator')
        print('running loss: {}'.format(self.policy.running_loss))  # TODO: remove
        execution_list = [
            self.discriminator_step_prediction,
            self.discriminator_loss,
            self.discriminator_accuracy_for_discriminator
        ]
        pred, loss, acc = sess.run(execution_list, feed_dictionary)
        print('pred: {}'.format(pred))
        print('acc: {}'.format(acc))
        print('loss: {}'.format(loss))
        if self.policy.should_train_discriminator(global_step, epoch_num, batch_index, pred, loss, acc):
            # the discriminator is still improving
            print('new running loss: {}'.format(self.policy.running_loss))  # TODO: remove
            print()
            sess.run(self.discriminator_train_step, feed_dictionary)
        else:
            print('discriminator too good - training generator')
            print()
            # the discriminator is no longer improving, will train generator next
            self.policy.do_train_switch(start_training_generator=True)
            self.do_generator_train(sess, global_step, epoch_num, batch_index, feed_dictionary)

    def do_before_train_loop(self, sess):
        sess.run(self.embedding_translator.assign_embedding(), {
            self.embedding_translator.embedding_placeholder: self.embedding_handler.embedding_np
        })
        self.policy.do_train_switch(start_training_generator=False)

    def do_train_batch(self, sess, global_step, epoch_num, batch_index, batch):
        feed_dict = {
            self.left_padded_source_batch: batch[0].left_padded_sentences,
            self.left_padded_target_batch: batch[1].left_padded_sentences,
            self.right_padded_source_batch: batch[0].right_padded_sentences,
            self.right_padded_target_batch: batch[1].right_padded_sentences,
            self.dropout_placeholder: self.config['model']['dropout'],
            self.discriminator_dropout_placeholder: self.config['model']['discriminator_dropout'],
            self.encoder.should_print: self.operational_config['debug'],
            self.decoder.should_print: self.operational_config['debug'],
            self.discriminator.should_print: self.operational_config['debug'],
            self.embedding_translator.should_print: self.operational_config['debug'],
        }
        print('batch len: {}'.format(batch[0].get_len()))
        if self.policy.train_generator:
            # should train the generator
            return self.do_generator_train(sess, global_step, epoch_num, batch_index, feed_dict)
        else:
            # should train discriminator
            return self.do_discriminator_train(sess, global_step, epoch_num, batch_index, feed_dict)

    # def do_validation_batch(self, sess, global_step, epoch_num, batch_index, batch):
        # feed_dict = {
        #     self.left_padded_source_batch: batch[0].left_padded_sentences,
        #     self.dropout_placeholder: 0.0,
        #     self.encoder.should_print: self.operational_config['debug'],
        #     self.decoder.should_print: self.operational_config['debug'],
        #     self.embedding_translator.should_print: self.operational_config['debug'],
        # }
        # transferred_result = sess.run(self.transfer, feed_dict)
        # end_of_sentence_index = self.embedding_handler.word_to_index[self.embedding_handler.end_of_sentence_token]
        # # only take the prefix before EOS:
        # transferred_result = [s[:s.tolist().index(end_of_sentence_index) + 1] for s in transferred_result if
        #                      end_of_sentence_index in s]
        # # print the transfer
        # self.print_side_by_side(
        #     self.remove_by_mask(batch[0].right_padded_sentences, batch[0].right_padded_masks),
        #     transfered_result,
        #     'original: ',
        #     'transferred: ',
        #     self.embedding_handler
        # )
        # print the accuracy traces:

    def do_after_train_loop(self, sess):
        pass

    def do_before_epoch(self, sess):
        pass

    def do_after_epoch(self, sess):
        pass


if __name__ == "__main__":
    with open("config/gan.yml", 'r') as ymlfile:
        config = yaml.load(ymlfile)
    with open("config/operational.yml", 'r') as ymlfile:
        operational_config = yaml.load(ymlfile)

    ModelTrainer(config, operational_config).do_train_loop()
