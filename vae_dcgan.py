"""
Andrin Jenal, 2017
ETH Zurich
"""


import tensorflow as tf
import numpy as np

import nn_ops
from vgg import vgg


class VAE_DCGAN:

    def __init__(self, image_size, channels, z_size=256, learning_rate_enc=5e-4, learning_rate_dis=5e-4, learning_rate_dec=5e-4):
        # summaries
        self.merged_summary_op = None
        self.summary_writer = None

        self.image_size = image_size
        self.image_channels = channels
        self.z_size = z_size

        self.d_real = 0.0
        self.d_fake = 0.0

        self.learning_rate_enc = learning_rate_enc
        self.learning_rate_gen = learning_rate_dec
        self.learning_rate_dis = learning_rate_dis

        self.eps = 1e-8

        self.lr_discriminator = tf.placeholder(tf.float32, shape=[])
        self.lr_generator = tf.placeholder(tf.float32, shape=[])
        self.lr_encoder = tf.placeholder(tf.float32, shape=[])

        self.x = tf.placeholder(tf.float32, shape=(None, self.image_size, self.image_size, self.image_channels))
        self.batch_size = tf.shape(self.x)[0]

        self.z_p = tf.random_normal((self.batch_size, self.z_size), mean=0, stddev=1)
        self.eps = tf.random_normal((self.batch_size, self.z_size), mean=0, stddev=1)

        # preload vgg network for loss backpropagation
        self.vgg_weights, self.vgg_mean_pixel = vgg.load_net("datasets/imagenet-vgg-verydeep-19.mat")

        with tf.variable_scope("vae_dcgan_model"):
            tf.summary.histogram("x_values", self.x)

            with tf.variable_scope("encoder"):
                self.z_x_mean, self.z_x_log_sigma = self._encoder(self.x)

            with tf.variable_scope("generator"):
                self.z_x = tf.add(self.z_x_mean, tf.multiply(tf.exp(self.z_x_log_sigma), self.eps))
                tf.summary.histogram("z", self.z_x)
                tf.summary.histogram("z_mu", self.z_x_mean)
                tf.summary.histogram("z_sigma", tf.exp(self.z_x_log_sigma))

                self.x_tilde = self._generator(self.z_x)
                tf.summary.histogram("x_tilde_values", self.x_tilde)

            with tf.variable_scope("discriminator"):
                self.dis_x_tilde_p, self.l_x_tilde = self._discriminator(self.x_tilde)
                tf.summary.histogram("predicted_x_tilde_values", self.dis_x_tilde_p)

            with tf.variable_scope("generator", reuse=True):
                self.x_p = self._generator(self.z_p)
                tf.summary.histogram("x_p_values", self.x_tilde)

            with tf.variable_scope("discriminator", reuse=True):
                self.dis_x, self.l_x = self._discriminator(self.x)
                tf.summary.histogram("predicted_x_values", self.dis_x)

            with tf.variable_scope("discriminator", reuse=True):
                self.dis_x_p, _ = self._discriminator(self.x_p)
                tf.summary.histogram("predicted_x_p_values", self.dis_x_p)

            with tf.variable_scope("losses"):
                self.prior = self._kl_divergence()

                self.discriminator_loss = self._wasserstein_gradient_penalty_discriminator_loss()
                self.generator_loss = self._wasserstein_gradient_penalty_generator_loss()
                self.lth_layer_loss = self._lth_layer_loss()
                self.mse_loss = self._pixel_loss()
                #self.feature_loss = self._vgg_feature_loss()

                self.dissimilarity_loss = 0.5 * self.lth_layer_loss + 0.5 * self.mse_loss

                self.loss_encoder = self.prior + self.dissimilarity_loss
                self.loss_generator = self.dissimilarity_loss + self.generator_loss
                self.loss_discriminator = self.discriminator_loss

                train_variables = tf.trainable_variables()
                self.encoder_vars = [var for var in train_variables if "encoder" in var.name]
                self.discriminator_vars = [var for var in train_variables if "discriminator" in var.name]
                self.generator_vars = [var for var in train_variables if "generator" in var.name]

                with tf.name_scope("encoder_optimizer"):
                    self.e_optim = self._adam_optimizer(self.loss_encoder, self.encoder_vars, self.lr_encoder)

                with tf.name_scope("discriminator_optimizer"):
                    self.d_optim = self._rms_prop_optimizer(self.loss_discriminator, self.discriminator_vars, self.lr_discriminator)

                with tf.name_scope("generator_optimizer"):
                    self.g_optim = self._rms_prop_optimizer(self.loss_generator, self.generator_vars, self.lr_generator)

        # initialize saver
        self.saver = tf.train.Saver([v for v in tf.global_variables() if "vae_dcgan_model" in v.name])

        self._check_tensors()

    def _adam_optimizer(self, loss, loss_params, learning_rate, beta1=0.5):
        optimizer = tf.train.AdamOptimizer(learning_rate, beta1=beta1)
        grads = optimizer.compute_gradients(loss, var_list=loss_params)
        grads = nn_ops.clip_gradient_norms(grads, 50)
        train_optimizer = optimizer.apply_gradients(grads)
        grad_norms = self._l2_norms(grads)
        tf.summary.histogram("gradient_l2_norms", grad_norms)
        return train_optimizer

    def _rms_prop_optimizer(self, loss, loss_params, learning_rate):
        optimizer = tf.train.RMSPropOptimizer(learning_rate)
        grads = optimizer.compute_gradients(loss, var_list=loss_params)
        grads = nn_ops.clip_gradient_norms(grads, 50)
        train_optimizer = optimizer.apply_gradients(grads)
        grad_norms = self._l2_norms(grads)
        tf.summary.histogram("gradient_l2_norms", grad_norms)
        return train_optimizer

    def _l2_norms(self, gradients):
        return [tf.nn.l2_loss(g) for g, v in gradients if g is not None]

    def _encoder2(self, x):
        x = tf.reshape(x, [self.batch_size, self.image_size * self.image_size * self.image_channels])
        z_mean = nn_ops.linear_contrib(x, self.z_size, activation_fn=None, scope="fully_connected")
        z_log_sigma_sq = nn_ops.linear_contrib(x, self.z_size, activation_fn=None, scope="fully_connected")
        return z_mean, z_log_sigma_sq

    def _encoder(self, x):
        x = tf.reshape(x, [self.batch_size, self.image_size, self.image_size, self.image_channels])
        conv1 = nn_ops.conv2d_contrib(x, 64, kernel=5, stride=2, activation_fn=nn_ops.relu_batch_norm, scope="conv1")
        conv2 = nn_ops.conv2d_contrib(conv1, 128, kernel=3, stride=2, activation_fn=nn_ops.relu_batch_norm, scope="conv2")
        conv3 = nn_ops.conv2d_contrib(conv2, 256, kernel=3, stride=2, padding="VALID", activation_fn=nn_ops.relu_batch_norm, scope="conv3")
        flatten = nn_ops.flatten_contrib(conv3)
        z_mean = nn_ops.linear_contrib(flatten, self.z_size, activation_fn=nn_ops.relu_batch_norm, scope="fully_connected")
        z_log_sigma = nn_ops.linear_contrib(flatten, self.z_size, activation_fn=nn_ops.relu_batch_norm, scope="fully_connected")
        return z_mean, z_log_sigma

    def _discriminator(self, x):
        x = tf.reshape(x, [self.batch_size, self.image_size, self.image_size, self.image_channels])
        conv1 = nn_ops.conv2d_contrib(x, 64, kernel=5, stride=2, activation_fn=nn_ops.leaky_relu_batch_norm, scope="conv1")
        conv2 = nn_ops.conv2d_contrib(conv1, 128, kernel=3, stride=2, activation_fn=nn_ops.leaky_relu_batch_norm, scope="conv2")
        conv3 = nn_ops.conv2d_contrib(conv2, 256, kernel=3, stride=2, activation_fn=nn_ops.leaky_relu_batch_norm, scope="conv3")
        conv4 = nn_ops.conv2d_contrib(conv3, 256, kernel=3, stride=2, padding="VALID", activation_fn=nn_ops.leaky_relu_batch_norm, scope="conv4")
        conv4 = nn_ops.flatten_contrib(conv4)
        fc = nn_ops.linear_contrib(conv4, 512, activation_fn=nn_ops.leaky_relu_batch_norm, scope="fully_connected")
        predicted = nn_ops.linear_contrib(fc, 1, activation_fn=tf.nn.sigmoid, scope="prediction")
        net = {"conv1": conv1, "conv2": conv2, "conv3": conv3, "conv4": conv4, "fc": fc}
        return predicted, [net["conv1"], net["conv2"], net["fc"]]

    def _generator(self, z):
        fc = nn_ops.linear_contrib(z, 4 * 4 * 512, activation_fn=None)
        z = tf.reshape(fc, shape=(tf.shape(z)[0], 4, 4, 512))
        deconv1 = nn_ops.conv2d_transpose_contrib(z, 512, kernel=4, stride=2, activation_fn=nn_ops.relu_batch_norm, scope="upconv1")
        deconv2 = nn_ops.conv2d_transpose_contrib(deconv1, 256, kernel=5, stride=2, activation_fn=nn_ops.relu_batch_norm, scope="upconv2")
        deconv3 = nn_ops.conv2d_transpose_contrib(deconv2, 128, kernel=5, stride=2, activation_fn=nn_ops.relu_batch_norm, scope="upconv3")
        deconv4 = nn_ops.conv2d_transpose_contrib(deconv3, self.image_channels, kernel=5, stride=2, activation_fn=None, scope="upconv4")
        return tf.nn.tanh(deconv4)

    def _discriminator_loss(self, eps=1e-8):
        with tf.name_scope("logits_discriminator_loss"):
            dis_loss = tf.reduce_mean(-1.0 * tf.log(tf.clip_by_value(self.dis_x, eps, 1.0)) -
                                      tf.log(tf.clip_by_value(1.0 - self.dis_x_p, eps, 1.0)) -
                                      tf.log(tf.clip_by_value(1.0 - self.dis_x_tilde_p, eps, 1.0)))
            tf.summary.scalar("discriminator_loss_mean", dis_loss)
            return dis_loss

    def _generator_loss(self, eps=1e-8):
        with tf.name_scope("logits_generator_loss"):
            gen_loss = tf.reduce_mean(-1.0 * tf.log(tf.clip_by_value(self.dis_x_p, eps, 1.0)) -
                                      tf.log(tf.clip_by_value(self.dis_x_tilde_p, eps, 1.0)))
            tf.summary.scalar("generator_loss_mean", gen_loss)
            return gen_loss

    def _kl_divergence(self):
        with tf.name_scope("kl_divergence_loss"):
            KL = tf.reduce_sum((-self.z_x_log_sigma + 0.5 * (tf.exp(2.0 * self.z_x_log_sigma) + tf.square(self.z_x_mean)) - 0.5), axis=-1)
            KL_mean = tf.reduce_mean(KL)
            tf.summary.histogram("KL_divergence", KL)
            tf.summary.scalar("kl_divergence_mean", KL_mean)
            return KL_mean

    def _pixel_loss(self):
        with tf.name_scope("pixel_loss"):
            pixel_loss = tf.reduce_mean(tf.nn.l2_loss(self.x - self.x_tilde) / np.prod(self.x.get_shape().as_list()[1:]))
            pixel_loss_weighted = 1.0 * pixel_loss
            tf.summary.scalar("pixel_loss_mean", pixel_loss_weighted)
            return pixel_loss_weighted

    def _lth_layer_loss(self):
        with tf.name_scope("lth_layer_loss"):
            lth_layer_loss = 0
            for l1, l2 in zip(self.l_x, self.l_x_tilde):
                lth_layer_loss += tf.nn.l2_loss((l1 - l2)) / (self.image_size * self.image_size * self.image_channels)
            lth_layer_loss *= 1.0 / len(self.l_x)
            tf.summary.scalar("lth_layer_loss_mean", lth_layer_loss)
            return lth_layer_loss

    def _discriminator_binary_cross_entropy_loss(self):
        with tf.name_scope("cross_entropy_discriminator_loss"):
            d_loss_real = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=self.dis_x, labels=tf.ones_like(self.dis_x)))
            d_loss_fake = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=self.dis_x_p, labels=tf.zeros_like(self.dis_x_p)))
            dis_loss = d_loss_real + d_loss_fake
            tf.summary.scalar("discriminator_loss_mean", dis_loss)
            return dis_loss

    def _generator_binary_cross_entropy_loss(self):
        with tf.name_scope("cross_entropy_generator_loss"):
            gen_loss = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=self.dis_x_p, labels=tf.ones_like(self.dis_x_p)))
            tf.summary.scalar("generator_loss_mean", gen_loss)
            return gen_loss

    def _wasserstein_discriminator_loss(self):
        """ https://github.com/igul222/improved_wgan_training/blob/master/gan_mnist.py """
        with tf.name_scope("wasserstein_discriminator_loss"):
            dis_loss = -tf.reduce_mean(self.dis_x) + tf.reduce_mean(self.dis_x_p) + tf.reduce_mean(self.dis_x_tilde_p)
            tf.summary.scalar("discriminator_loss_mean", dis_loss)
            return dis_loss

    def _wasserstein_generator_loss(self):
        """ https://github.com/igul222/improved_wgan_training/blob/master/gan_mnist.py """
        with tf.name_scope("wasserstein_generator_loss"):
            gen_loss = -tf.reduce_mean(self.dis_x_p) - tf.reduce_mean(self.dis_x_tilde_p)
            tf.summary.scalar("generator_loss_mean", gen_loss)
            return gen_loss

    def _wasserstein_gradient_penalty_discriminator_loss(self):
        """ https://github.com/igul222/improved_wgan_training/blob/master/gan_mnist.py """
        with tf.name_scope("wasserstein_gradient_penalty_discriminator_loss"):
            dis_loss = -tf.reduce_mean(self.dis_x) + tf.reduce_mean(self.dis_x_p) + tf.reduce_mean(self.dis_x_tilde_p)
            x_p = tf.reshape(self.x_p, [self.batch_size, -1])
            x = tf.reshape(self.x, [self.batch_size, -1])
            differences = x_p - x
            interpolates = x + (tf.random_uniform([self.batch_size, 1], minval=0, maxval=1) * differences)
            dis_interpolates, _ = self._discriminator(interpolates)
            gradients = tf.gradients(dis_interpolates, [interpolates])[0]
            slopes = tf.sqrt(tf.reduce_sum(tf.square(gradients), reduction_indices=[1]))
            gradient_penalty = tf.reduce_mean((slopes - 1.) ** 2)
            dis_loss += 10 * gradient_penalty
            tf.summary.scalar("discriminator_loss_mean", dis_loss)
            return dis_loss

    def _wasserstein_gradient_penalty_generator_loss(self):
        """ https://github.com/igul222/improved_wgan_training/blob/master/gan_mnist.py """
        with tf.name_scope("wasserstein_gradient_penalty_generator_loss"):
            gen_loss = -tf.reduce_mean(self.dis_x_p) - tf.reduce_mean(self.dis_x_tilde_p)
            tf.summary.scalar("generator_loss_mean", gen_loss)
            return gen_loss

    def _vgg_feature_loss(self):
        with tf.name_scope("feature_loss_vgg"):
            feature_layers = ["relu3_3"]
            # [0.22591736, 0.77408264]
            # [0.09079630, 0.33333333, 0.57587037]
            # [0.04912966, 0.16162175, 0.33837825, 0.45087034]
            # [0.03139669, 0.09036695, 0.20000000, 0.30963305, 0.36860331]
            # [0.02222448, 0.05677295, 0.12367752, 0.20965582, 0.27656038, 0.31110886]
            # [0.01683352, 0.03891270, 0.08120526, 0.14285714, 0.20450902, 0.24680159, 0.26888077]
            # [0.01336953, 0.02844961, 0.05647934, 0.09969787, 0.15030213, 0.19352066, 0.22155039, 0.23663047]
            feature_weights = [1.0]
            feature_losses = []
            for ith, layer in enumerate(feature_layers):
                x_weights, _ = self._vgg_layer_weights(self.x, layer, self.batch_size, self.image_size, self.image_size, self.vgg_weights, self.vgg_mean_pixel)
                x_tilde_weights, x_tilde_size = self._vgg_layer_weights(self.x_tilde, layer, self.batch_size, self.image_size, self.image_size, self.vgg_weights, self.vgg_mean_pixel)
                feature_losses.append(feature_weights[ith] * (tf.nn.l2_loss(x_weights - x_tilde_weights) / x_tilde_size))
            feature_loss = tf.reduce_mean(tf.convert_to_tensor(feature_losses))
            feature_loss_weighted = feature_loss
            tf.summary.scalar("feature_loss_mean", feature_loss_weighted)
            return feature_loss_weighted

    def _vgg_layer_weights(self, input_images, layer_name, batch_size, image_height, image_width, vgg_weights, vgg_mean_pixel, pooling="avg"):
        if self.image_channels == 3:
            input_images = tf.reshape(input_images, shape=[batch_size, image_height, image_width, self.image_channels])
        else:
            input_images = tf.reshape(input_images, shape=[batch_size, image_height, image_width])
            input_images = tf.stack([input_images, input_images, input_images], axis=-1)
        input_images_mean = vgg.preprocess(input_images, vgg_mean_pixel)
        net_forward_images = vgg.net_preloaded(vgg_weights, input_images_mean, pooling)
        weights_size = np.prod(net_forward_images[layer_name].get_shape().as_list()[1:])
        return net_forward_images[layer_name], weights_size

    def _check_tensors(self):
        if tf.trainable_variables():
            for v in tf.trainable_variables():
                print("%s : %s" % (v.name, v.get_shape()))

    def _sigmoid(self, x, shift, mult):
        """
        Using this sigmoid to discourage one network overpowering the other
        """
        return 1 / (1 + np.exp(-(x + shift) * mult))

    def update_params(self, sess, input_tensor):
        e_current_lr = self.learning_rate_enc# * self._sigmoid(np.mean(self.d_real), -.5, 15)
        g_current_lr = self.learning_rate_gen# * self._sigmoid(np.mean(self.d_real), -.5, 15)
        d_current_lr = self.learning_rate_dis# * self._sigmoid(np.mean(self.d_fake), -.5, 15)

        _, _, _ = sess.run([self.d_optim, self.e_optim, self.g_optim], feed_dict={self.x: input_tensor,
                                                                                  self.lr_encoder: e_current_lr,
                                                                                  self.lr_generator: g_current_lr,
                                                                                  self.lr_discriminator: d_current_lr})

        kl, d_loss, g_loss, lth_layer, d_real, d_fake = sess.run([self.prior, self.discriminator_loss, self.generator_loss, self.dissimilarity_loss, self.dis_x, self.dis_x_p], feed_dict={self.x: input_tensor})

        self.d_real = d_real
        self.d_fake = d_fake

        return kl, d_loss, g_loss, lth_layer, e_current_lr, g_current_lr, d_current_lr

    def generate_samples(self, sess, num_samples):
        z = np.random.normal(size=(num_samples, self.z_size))
        samples = sess.run(self.x_p, feed_dict={self.z_p: z})
        return np.array(samples)

    def initialize_summaries(self, sess, summary_directory):
        self.merged_summary_op = tf.summary.merge_all()
        self.summary_writer = tf.summary.FileWriter(summary_directory, sess.graph)

    def update_summaries(self, sess, x, epoch):
        if self.merged_summary_op is not None:
            summary = sess.run(self.merged_summary_op, feed_dict={self.x: x})
            self.summary_writer.add_summary(summary, global_step=epoch)
            print("updated summaries...")

    def restore_model(self, sess, checkpoint_file):
        self.saver.restore(sess, checkpoint_file)
        print("model restored from:", checkpoint_file)
