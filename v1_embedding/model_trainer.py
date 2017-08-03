import tensorflow as tf
from datasets.yelp_helpers import YelpSentences
from v1_embedding.embedding_translator import EmbeddingTranslator
from v1_embedding.embedding_encoder import EmbeddingEncoder
from v1_embedding.embedding_decoder import EmbeddingDecoder
from v1_embedding.embedding_discriminator import EmbeddingDiscriminator
from v1_embedding.loss_handler import LossHandler
from v1_embedding.model_trainer_base import ModelTrainerBase
from v1_embedding.word_indexing_embedding_handler import WordIndexingEmbeddingHandler


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

        self.source_identifier = tf.ones(shape=())
        self.target_identifier = -1 * tf.ones(shape=())

        self.dataset_neg = YelpSentences(positive=False, limit_sentences=self.config['sentence']['limit'],
                                         dataset_cache_dir=self.dataset_cache_dir, dataset_name='neg')
        self.dataset_pos = YelpSentences(positive=True, limit_sentences=self.config['sentence']['limit'],
                                         dataset_cache_dir=self.dataset_cache_dir, dataset_name='pos')
        self.embedding_handler = WordIndexingEmbeddingHandler(
            self.embedding_dir,
            [self.dataset_neg, self.dataset_pos],
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
        self.target_adversarial_loss = self.get_discriminator_loss(self.left_padded_source_batch,
                                                                   self.left_padded_target_batch,
                                                                   self.right_padded_target_batch,
                                                                   self.target_identifier)
        self.source_adversarial_loss = self.get_discriminator_loss(self.left_padded_target_batch,
                                                                   self.left_padded_source_batch,
                                                                   self.right_padded_source_batch,
                                                                   self.source_identifier)

        self.target_generator_loss = self.get_generator_loss(self.left_padded_source_batch,
                                                             self.right_padded_source_batch,
                                                             self.left_padded_target_batch,
                                                             self.right_padded_target_batch,
                                                             self.target_identifier)
        self.source_generator_loss = self.get_generator_loss(self.left_padded_target_batch,
                                                             self.right_padded_target_batch,
                                                             self.left_padded_source_batch,
                                                             self.right_padded_source_batch,
                                                             self.source_identifier)
        # train steps
        discriminator_optimizer = tf.train.GradientDescentOptimizer(self.config['model']['learn_rate'])
        discriminator_var_list = self.discriminator.get_trainable_parameters()
        discriminator_grads_and_vars = discriminator_optimizer.compute_gradients(
            self.source_adversarial_loss + self.target_adversarial_loss,
            colocate_gradients_with_ops=True, var_list=discriminator_var_list
        )
        self.discriminator_train_step = discriminator_optimizer.apply_gradients(discriminator_grads_and_vars)

        generator_optimizer = tf.train.GradientDescentOptimizer(self.config['model']['learn_rate'])
        generator_var_list = self.encoder.get_trainable_parameters() + self.decoder.get_trainable_parameters() + \
                             self.embedding_translator.get_trainable_parameters()
        generator_grads_and_vars = generator_optimizer.compute_gradients(
            self.target_generator_loss + self.source_generator_loss,
            colocate_gradients_with_ops=True, var_list=generator_var_list
        )
        self.generator_train_step = generator_optimizer.apply_gradients(generator_grads_and_vars)

    @staticmethod
    def _get_discriminator_index(identifier):
        # if identifier is 1 index is 0, if identifier is -1 index is 1
        return 0.5 * (1 - identifier)

    def _get_discriminator_loss_from_encoded(self, encoded_source, encoded_target, right_padded_target_embbeding,
                                             target_identifier, sentence_length):
        descriminator_index = self._get_discriminator_index(target_identifier)
        # calculate the teacher forced loss
        teacher_forced_target = self.decoder.do_teacher_forcing(encoded_target,
                                                                right_padded_target_embbeding[:, :-1, :],
                                                                target_identifier)
        discriminator_prediction_target = self.discriminator.predict(teacher_forced_target)
        loss_true = -tf.reduce_mean(
            tf.log(discriminator_prediction_target[:, descriminator_index])
        )
        # calculate the source-encoded-as-target loss
        fake_targets = self.decoder.do_iterative_decoding(encoded_source, target_identifier, sentence_length)
        discriminator_prediction_fake_target = self.discriminator.predict(fake_targets)
        loss_fake = -tf.reduce_mean(
            tf.log(1.0 - discriminator_prediction_fake_target[:, descriminator_index])
        )
        return loss_true + loss_fake

    def get_discriminator_loss(self, left_padded_source_batch, left_padded_target_batch, right_padded_target_batch,
                               target_identifier):
        sentence_length = tf.shape(left_padded_source_batch)[1]

        source_embedding = self.embedding_translator.embed_inputs(left_padded_source_batch)
        encoded_source = self.encoder.encode_inputs_to_vector(source_embedding, -1*target_identifier)

        left_padded_target_embedding = self.embedding_translator.embed_inputs(left_padded_target_batch)
        encoded_target = self.encoder.encode_inputs_to_vector(left_padded_target_embedding, target_identifier)

        right_padded_target_embedding = self.embedding_translator.embed_inputs(right_padded_target_batch)

        return self._get_discriminator_loss_from_encoded(encoded_source, encoded_target, right_padded_target_embedding,
                                             target_identifier, sentence_length)

    def get_generator_loss(self, left_padded_source_batch, right_padded_source_batch, left_padded_target_batch,
                           right_padded_target_batch, target_identifier):
        source_identifier = -1*target_identifier
        sentence_length = tf.shape(left_padded_source_batch)[1]

        left_padded_source_embedding = self.embedding_translator.embed_inputs(left_padded_source_batch)
        encoded_source = self.encoder.encode_inputs_to_vector(left_padded_source_embedding, source_identifier)

        left_padded_target_embedding = self.embedding_translator.embed_inputs(left_padded_target_batch)
        encoded_target = self.encoder.encode_inputs_to_vector(left_padded_target_embedding, target_identifier)

        # reconstruction loss
        right_padded_source_embedding = self.embedding_translator.embed_inputs(right_padded_source_batch)
        source_decoded_to_source = self.decoder.do_teacher_forcing(encoded_source,
                                                                   right_padded_source_embedding[:, :-1, :],
                                                                   source_identifier)
        reconstructed_source_logits = self.embedding_translator.translate_embedding_to_vocabulary_logits(
            source_decoded_to_source)
        reconstruction_loss = self.loss_handler.get_sentence_reconstruction_loss(right_padded_source_batch,
                                                                                 reconstructed_source_logits)

        # semantic vector distance
        encoded_unstacked = tf.unstack(encoded_source)
        processed_encoded = []
        for e in encoded_unstacked:
            d = self.decoder.do_iterative_decoding(e, target_identifier, iterations_limit=-1)
            e_target = self.encoder.encode_inputs_to_vector(d, target_identifier)
            processed_encoded.append(e_target)
        encoded_again = tf.concat(processed_encoded, axis=0)
        semantic_distance_loss = self.loss_handler.get_context_vector_distance_loss(encoded_source, encoded_again)

        # professor forcing loss source
        right_padded_target_embedding = self.embedding_translator.embed_inputs(right_padded_target_batch)
        anti_d_loss = -self._get_discriminator_loss_from_encoded(encoded_source, encoded_target,
                                                                 right_padded_target_embedding, target_identifier,
                                                                 sentence_length)

        return self.config['reconstruction_coefficient'] * reconstruction_loss \
               + self.config['semantic_distance_coefficient'] * semantic_distance_loss \
               + anti_d_loss
