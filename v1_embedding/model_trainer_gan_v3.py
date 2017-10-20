import yaml
import datetime
import os
from datasets.multi_batch_iterator import MultiBatchIterator
from datasets.yelp_helpers import YelpSentences
from v1_embedding.gan_model import GanModel
from collections import Counter
from v1_embedding.logger import init_logger
from v1_embedding.model_trainer_base import ModelTrainerBase
from v1_embedding.pre_trained_embedding_handler import PreTrainedEmbeddingHandler
from datasets.classify_sentiment import classify


class ModelTrainerGan(ModelTrainerBase):
    def __init__(self, config_file, operational_config_file):
        ModelTrainerBase.__init__(self, config_file=config_file, operational_config_file=operational_config_file)

        self.dataset_neg = YelpSentences(positive=False,
                                         limit_sentences=self.config['sentence']['limit'],
                                         validation_limit_sentences=self.config['sentence']['validation_limit'],
                                         dataset_cache_dir=self.get_dataset_cache_dir(),
                                         dataset_name='neg')
        self.dataset_pos = YelpSentences(positive=True,
                                         limit_sentences=self.config['sentence']['limit'],
                                         validation_limit_sentences=self.config['sentence']['validation_limit'],
                                         dataset_cache_dir=self.get_dataset_cache_dir(),
                                         dataset_name='pos')
        datasets = [self.dataset_neg, self.dataset_pos]
        self.embedding_handler = PreTrainedEmbeddingHandler(
            self.get_embedding_dir(),
            datasets,
            self.config['embedding']['word_size'],
            self.config['embedding']['min_word_occurrences']
        )

        contents, validation_contents = MultiBatchIterator.preprocess(datasets)
        # iterators
        self.batch_iterator = MultiBatchIterator(contents,
                                                 self.embedding_handler,
                                                 self.config['sentence']['min_length'],
                                                 self.config['trainer']['batch_size'])

        # iterators
        self.batch_iterator_validation = MultiBatchIterator(validation_contents,
                                                            self.embedding_handler,
                                                            self.config['sentence']['min_length'],
                                                            self.config['trainer']['validation_batch_size'])
        # set the model
        self.model = GanModel(self.config, self.operational_config, self.embedding_handler)

    def get_trainer_name(self):
        return '{}_{}'.format(self.__class__.__name__, self.config['model']['discriminator_type'])

    def transfer_batch(self, sess, batch, epoch_num, return_result_as_summary=True, print_to_file=False):
        feed_dict = {
            self.model.source_batch: batch[0].sentences,
            self.model.target_batch: batch[1].sentences,
            self.model.source_lengths: batch[0].lengths,
            self.model.target_lengths: batch[1].lengths,
            self.model.dropout_placeholder: 0.0,
            self.model.discriminator_dropout_placeholder: 0.0,
        }
        transferred_result, reconstruction_result = sess.run(
            [self.model.transferred_source_batch, self.model.reconstructed_targets_batch], feed_dict
        )
        end_of_sentence_index = self.embedding_handler.word_to_index[self.embedding_handler.end_of_sentence_token]
        # original source without paddings:
        original_source = self.remove_by_length(batch[0].sentences, batch[0].lengths)
        # original target without paddings:
        original_target = self.remove_by_length(batch[1].sentences, batch[1].lengths)
        # only take the prefix before EOS:
        transferred = []
        for s in transferred_result:
            if end_of_sentence_index in s:
                transferred.append(s[:s.tolist().index(end_of_sentence_index) + 1])
            else:
                transferred.append(s)
        reconstructed = []
        for s in reconstruction_result:
            if end_of_sentence_index in s:
                reconstructed.append(s[:s.tolist().index(end_of_sentence_index) + 1])
            else:
                reconstructed.append(s)
        if print_to_file:
            now = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            log_file_name = os.path.join('logs', '{}-epoch-{}.log'.format(now, epoch_num))
            original_source_strings, transferred_strings = self.print_to_file(
                original_source,
                transferred,
                self.embedding_handler,
                log_file_name
            )
        else:
            # print the reconstruction
            original_target_strings, reconstructed_strings = self.print_side_by_side(
                original_target,
                reconstructed,
                'original_target: ',
                'reconstructed: ',
                self.embedding_handler
            )
            # print the transfer
            original_source_strings, transferred_strings = self.print_side_by_side(
                original_source,
                transferred,
                'original_source: ',
                'transferred: ',
                self.embedding_handler
            )
        #evaluate the transfer
        evaluation_prediction, evaluation_confidence = classify([' '.join(s) for s in transferred_strings])
        evaluation_accuracy = Counter(evaluation_prediction)['pos'] / float(len(evaluation_prediction))
        average_evaluation_confidence = sum(evaluation_confidence) / float(len(evaluation_confidence))
        if print_to_file:
            with open(os.path.join('logs', 'accuracy.log'), 'a+') as f:
                f.write('Date: {}, Epoch: {}, Acc: {}, Confidence: {}\n'.format(now, epoch_num, evaluation_accuracy,
                                                                                average_evaluation_confidence))
        else:
            print('Transferred evaluation acc: {} with average confidence of: {}'.format(
                evaluation_accuracy, average_evaluation_confidence)
            )

        if return_result_as_summary:
            return sess.run(
                self.model.evaluation_summary,
                {
                    self.model.text_watcher.placeholders['original_source']: [' '.join(s) for s in
                                                                              original_source_strings],
                    self.model.text_watcher.placeholders['original_target']: [' '.join(s) for s in
                                                                              original_target_strings],
                    self.model.text_watcher.placeholders['transferred']: [' '.join(s) for s in transferred_strings],
                    self.model.text_watcher.placeholders['reconstructed']: [' '.join(s) for s in reconstructed_strings],
                })
        else:
            return None

    def do_before_train_loop(self, sess):
        sess.run(self.model.embedding_container.assign_embedding(), {
            self.model.embedding_container.embedding_placeholder: self.embedding_handler.embedding_np
        })

    def do_train_batch(self, sess, global_step, epoch_num, batch_index, batch, extract_summaries=False):
        feed_dict = {
            self.model.source_batch: batch[0].sentences,
            self.model.target_batch: batch[1].sentences,
            self.model.source_lengths: batch[0].lengths,
            self.model.target_lengths: batch[1].lengths,
            self.model.dropout_placeholder: self.config['model']['dropout'],
            self.model.discriminator_dropout_placeholder: self.config['model']['discriminator_dropout'],
        }
        print('batch len: {}'.format(batch[0].get_len()))
        execution_list = [
            self.model.master_step,
            self.model.discriminator_loss,
            self.model.accuracy,
            self.model.train_generator,
            self.model.summary_step
        ]
        if extract_summaries:
            _, discriminator_loss, accuracy, train_generator_flag, summary = sess.run(execution_list, feed_dict)
        else:
            _, discriminator_loss, accuracy, train_generator_flag = sess.run(execution_list[:-1], feed_dict)
            summary = None
        print('accuracy: {}'.format(accuracy))
        print('discriminator_loss: {}'.format(discriminator_loss))
        print('training generator? {}'.format(train_generator_flag))
        return summary

    def do_validation_batch(self, sess, global_step, epoch_num, batch_index, batch, print_to_file):
        return self.transfer_batch(sess, batch, epoch_num, return_result_as_summary=not print_to_file,
                                   print_to_file=print_to_file)

    def do_after_train_loop(self, sess):
        # make sure the model is correct:
        self.saver_wrapper.load_model(sess)
        print('model loaded, sample sentences:')
        for batch in self.batch_iterator_validation:
            self.transfer_batch(sess, batch, 0, return_result_as_summary=False, print_to_file=False)
            break

    def do_before_epoch(self, sess, global_step, epoch_num):
        sess.run(self.model.assign_epoch, {self.model.epoch_placeholder: epoch_num})

    def do_after_epoch(self, sess, global_step, epoch_num):
        if epoch_num % 10 == 0:
            # activate the saver
            self.saver_wrapper.save_model(sess, global_step=global_step)


if __name__ == "__main__":
    with open("config/gan.yml", 'r') as ymlfile:
        config = yaml.load(ymlfile)
    with open("config/operational.yml", 'r') as ymlfile:
        operational_config = yaml.load(ymlfile)
    init_logger()
    print('------------ Config ------------')
    print(yaml.dump(config))
    print('------------ Operational Config ------------')
    print(yaml.dump(operational_config))
    ModelTrainerGan(config, operational_config).do_train_loop()
