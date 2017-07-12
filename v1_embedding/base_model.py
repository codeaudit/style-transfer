import tensorflow as tf


class BaseModel:
    def __init__(self):
        self.should_print = tf.placeholder_with_default(False, shape=())

    def print_tensor_with_shape(self, tensor, name):
        return tf.cond(self.should_print,
                       lambda: tf.Print(
                           tf.Print(tensor, [tensor], message=name + ":"),
                           [tf.shape(tensor)], message=name + " shape:"),
                       lambda: tf.identity(tensor))

    @staticmethod
    def create_input_parameters(input_size, output_size):
        w = tf.Variable(tf.random_normal(shape=(input_size, output_size)), dtype=tf.float32)
        b = tf.Variable(tf.random_normal(shape=(output_size,)), dtype=tf.float32)
        return w, b

    def get_trainable_parameters(self):
        pass

    def save_model(self, sess, name, only_values = False):
        saver = tf.train.Saver()
        # saver.save(sess, name, None, not only_values)
        saver.save(sess, name)

    def load_model(self, sess, name = 'latest'):
        loader = tf.train.import_meta_graph(name)
        if name=='latest':
            loader.restore(sess, tf.train.latest_checkpoint('./'))
        else:
            loader.restore(sess, './' + name)
