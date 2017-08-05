import tensorflow as tf
import time
from datasets.multi_batch_iterator import MultiBatchIterator
from datasets.yelp_helpers import YelpSentences
from v1_embedding.embedding_translator import EmbeddingTranslator
from v1_embedding.embedding_encoder import EmbeddingEncoder
from v1_embedding.embedding_decoder import EmbeddingDecoder
from v1_embedding.embedding_discriminator import EmbeddingDiscriminator
from v1_embedding.loss_handler import LossHandler
from v1_embedding.model_trainer_base import ModelTrainerBase
from v1_embedding.word_indexing_embedding_handler import WordIndexingEmbeddingHandler


# this model tries to transfer from one domain to another.
# 1. the encoder doesn't know the domain it is working on
# 2. traget are encoded and decoded (to target) then cross entropy loss is applied between the origin and the result
# 3. source is encoded decoded to traget and encoded again, then L2 loss is applied between the context vectors.
# 4. an adversarial component is trained to distinguish true target from transferred targets using professor forcing
class ModelTrainer(ModelTrainerBase):
    def __init__(self, config_file, operational_config_file):
        ModelTrainerBase.__init__(self, config_file=config_file, operational_config_file=operational_config_file)

        # placeholders for dropouts
        self.dropout_placeholder = tf.placeholder(tf.float32, shape=())
        self.discriminator_dropout_placeholder = tf.placeholder(tf.float32, shape=())
        # placeholder for source sentences (batch, time)=> index of word s.t the padding is on the left
        self.left_padded_source_batch = tf.placeholder(tf.int64, shape=(None, None))
        # placeholder for source sentences (batch, time)=> index of word s.t the padding is on the right
        self.right_padded_source_batch = tf.placeholder(tf.int64, shape=(None, None))
        # placeholder for target sentences (batch, time)=> index of word s.t the padding is on the left
        self.left_padded_target_batch = tf.placeholder(tf.int64, shape=(None, None))
        # placeholder for target sentences (batch, time)=> index of word s.t the padding is on the right
        self.right_padded_target_batch = tf.placeholder(tf.int64, shape=(None, None))

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
                                                        self.config['embedding']['should_train'])
        self.encoder = EmbeddingEncoder(self.config['model']['encoder_hidden_states'], self.dropout_placeholder,
                                        self.config['model']['bidirectional_encoder'])
        self.decoder = EmbeddingDecoder(self.embedding_handler.get_embedding_size(),
                                        self.config['model']['decoder_hidden_states'],
                                        self.embedding_translator, self.dropout_placeholder)
        self.discriminator = EmbeddingDiscriminator(self.config['model']['discriminator_hidden_states'],
                                                    self.config['model']['discriminator_dense_hidden_size'],
                                                    self.discriminator_dropout_placeholder,
                                                    self.config['model']['bidirectional_discriminator'])
        self.loss_handler = LossHandler()

        # losses:
        self.adversarial_loss = self.get_discriminator_loss(self.left_padded_source_batch,
                                                            self.left_padded_target_batch,
                                                            self.right_padded_target_batch
                                                            )

        self.generator_loss = self.get_generator_loss(self.left_padded_source_batch,
                                                      self.left_padded_target_batch,
                                                      self.right_padded_target_batch
                                                      )

        # train steps
        discriminator_optimizer = tf.train.GradientDescentOptimizer(self.config['model']['learn_rate'])
        discriminator_var_list = self.discriminator.get_trainable_parameters()
        discriminator_grads_and_vars = discriminator_optimizer.compute_gradients(
            self.adversarial_loss,
            colocate_gradients_with_ops=True, var_list=discriminator_var_list
        )
        self.discriminator_train_step = discriminator_optimizer.apply_gradients(discriminator_grads_and_vars)

        generator_optimizer = tf.train.GradientDescentOptimizer(self.config['model']['learn_rate'])
        generator_var_list = self.encoder.get_trainable_parameters() + self.decoder.get_trainable_parameters() + \
                             self.embedding_translator.get_trainable_parameters()
        generator_grads_and_vars = generator_optimizer.compute_gradients(
            self.generator_loss,
            colocate_gradients_with_ops=True, var_list=generator_var_list
        )
        self.generator_train_step = generator_optimizer.apply_gradients(generator_grads_and_vars)

        # iterators
        self.batch_iterator = MultiBatchIterator(datasets, self.embedding_handler,
                                                 self.config['sentence']['min_length'],
                                                 self.config['model']['batch_size'])

        # iterators
        self.validation_batch_iterator = MultiBatchIterator(datasets, self.embedding_handler,
                                                            self.config['sentence']['min_length'],
                                                            1000)
        # train loop parameters:
        self.best_loss = float('inf')
        self.generator_mode = True

    def _get_discriminator_loss_from_encoded(self, encoded_source, teacher_forced_target):
        sentence_length = tf.shape(teacher_forced_target)[1]

        # calculate the teacher forced loss
        discriminator_prediction_target = self.discriminator.predict(teacher_forced_target)
        loss_true = -tf.reduce_mean(tf.log(discriminator_prediction_target))

        # calculate the source-encoded-as-target loss
        fake_targets = self.decoder.do_iterative_decoding(encoded_source, domain_identifier=None,
                                                          iterations_limit=sentence_length)
        discriminator_prediction_fake_target = self.discriminator.predict(fake_targets)
        loss_fake = -tf.reduce_mean(tf.log(1.0 - discriminator_prediction_fake_target))
        return loss_true + loss_fake

    def get_discriminator_loss(self, left_padded_source_batch, left_padded_target_batch, right_padded_target_batch):
        source_embedding = self.embedding_translator.embed_inputs(left_padded_source_batch)
        encoded_source = self.encoder.encode_inputs_to_vector(source_embedding, domain_identifier=None)

        left_padded_target_embedding = self.embedding_translator.embed_inputs(left_padded_target_batch)
        encoded_target = self.encoder.encode_inputs_to_vector(left_padded_target_embedding, domain_identifier=None)

        right_padded_target_embedding = self.embedding_translator.embed_inputs(right_padded_target_batch)
        teacher_forced_target = self.decoder.do_teacher_forcing(encoded_target,
                                                                right_padded_target_embedding[:, :-1, :],
                                                                domain_identifier=None)

        return self._get_discriminator_loss_from_encoded(encoded_source, teacher_forced_target)

    def get_generator_loss(self, left_padded_source_batch, left_padded_target_batch, right_padded_target_batch):
        left_padded_source_embedding = self.embedding_translator.embed_inputs(left_padded_source_batch)
        encoded_source = self.encoder.encode_inputs_to_vector(left_padded_source_embedding, domain_identifier=None)

        left_padded_target_embedding = self.embedding_translator.embed_inputs(left_padded_target_batch)
        encoded_target = self.encoder.encode_inputs_to_vector(left_padded_target_embedding, domain_identifier=None)

        # reconstruction loss
        right_padded_target_embedding = self.embedding_translator.embed_inputs(right_padded_target_batch)
        teacher_forced_target = self.decoder.do_teacher_forcing(encoded_target,
                                                                right_padded_target_embedding[:, :-1, :],
                                                                domain_identifier=None)
        reconstructed_taret_logits = self.embedding_translator.translate_embedding_to_vocabulary_logits(
            teacher_forced_target)
        reconstruction_loss = self.loss_handler.get_sentence_reconstruction_loss(right_padded_target_batch,
                                                                                 reconstructed_taret_logits)

        # semantic vector distance
        encoded_unstacked = tf.unstack(encoded_source)
        processed_encoded = []
        for e in encoded_unstacked:
            d = self.decoder.do_iterative_decoding(e, domain_identifier=None, iterations_limit=-1)
            e_target = self.encoder.encode_inputs_to_vector(d, domain_identifier=None)
            processed_encoded.append(e_target)
        encoded_again = tf.concat(processed_encoded, axis=0)
        semantic_distance_loss = self.loss_handler.get_context_vector_distance_loss(encoded_source, encoded_again)

        # professor forcing loss source
        anti_d_loss = -self._get_discriminator_loss_from_encoded(encoded_source, teacher_forced_target)

        return self.config['reconstruction_coefficient'] * reconstruction_loss \
               + self.config['semantic_distance_coefficient'] * semantic_distance_loss \
               + anti_d_loss

    def do_before_train_loop(self, sess):
        sess.run(self.embedding_translator.assign_embedding(), {
            self.embedding_translator.embedding_placeholder: self.embedding_handler.embedding_np
        })

    def do_train_batch(self, sess, global_step, epoch_num, batch_index, batch):
        if self.generator_mode:
            feed_dict = {
                self.left_padded_source_batch: batch[0].left_padded_sentences,
                self.left_padded_target_batch: batch[1].left_padded_sentences,
                self.right_padded_source_batch: batch[0].right_padded_sentences,
                self.right_padded_target_batch: batch[1].right_padded_sentences,
                self.dropout_placeholder: self.config['model']['dropout'],
                self.discriminator_dropout_placeholder: self.config['model']['discriminator_dropout'],
                self.encoder.should_print: self.operational_config['debug'],
                self.decoder.should_print: self.operational_config['debug'],
            }
            # TODO: outputs to measure progress, summaries
            execution_list = [self.generator_train_step, self.generator_loss]
            # print results
            if batch_index % 100 == 0:
                # start_time = time.time()
                # _, generator_loss = sess.run(execution_list, feed_dict)
                # total_time = time.time() - start_time
                # self.print_side_by_side(batch.right_padded_sentences, decoded_output, batch.right_padded_masks)
                # print('epoch-index: {} batch-index: {} acc: {} loss: {} runtime: {}'.format(epoch_num, batch_index,
                #                                                                             batch_acc, loss_output,
                #                                                                             total_time))
                print()
            else:
                # will not run summaries
                _, loss_output, decoded_output, batch_acc = sess.run(execution_list[:-1], feed_dict)
            # TODO: train source->target and target->source
        else:
            # should train discriminator
            print()

    def do_validation_batch(self, sess, global_step, epoch_num, batch_index, batch):
        pass

    def do_after_train_loop(self, sess):
        pass

    def do_before_epoch(self, sess):
        pass

    def do_after_epoch(self, sess):
        pass
