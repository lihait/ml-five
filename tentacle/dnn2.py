import gc
import os

import psutil

import numpy as np
import tensorflow as tf
from tentacle.board import Board
from tentacle.data_set import DataSet
from tentacle.dnn import Pre
from tentacle.ds_loader import DatasetLoader


class DCNN2(Pre):
    def __init__(self, is_train=True, is_revive=False):
        super().__init__(is_train, is_revive)
        self.loader_train = DatasetLoader(Pre.DATA_SET_TRAIN)
        self.loader_valid = DatasetLoader(Pre.DATA_SET_VALID)
        self.loader_test = DatasetLoader(Pre.DATA_SET_TEST)
        self.observation = []
        self.ds_rl = []

    def diags(self, a):
        assert len(a.shape) == 2 and a.shape[0] == a.shape[1]
        valid = a.shape[0] - 5

        vecs = [a.diagonal(i) for i in range(-valid, valid + 1)]
        c = np.zeros((len(vecs), a.shape[0]))
        c[:, :] = -1
        for i, v in enumerate(vecs):
            c[i, :v.shape[0]] = v
        return c

    def regulate(self, a):
        md = self.diags(a)
        ad = self.diags(np.rot90(a))
        m = np.vstack((a, a.T, md, ad))
        return m

    def placeholder_inputs(self):
        h, w, c = self.get_input_shape()
        states = tf.placeholder(tf.float32, [None, h, w, c])  # NHWC
        actions = tf.placeholder(tf.float32, [None, Pre.NUM_ACTIONS])
        return states, actions

    def model(self, states_pl, actions_pl):
        ch1 = 32
        W_1 = self.weight_variable([1, 5, Pre.NUM_CHANNELS, ch1])
        b_1 = self.bias_variable([ch1])

        ch = 32
        W_2 = self.weight_variable([3, 3, ch1, ch])
        b_2 = self.bias_variable([ch])
        W_21 = self.weight_variable([3, 3, ch, ch])
        b_21 = self.bias_variable([ch])

        self.h_conv1 = tf.nn.relu(tf.nn.conv2d(states_pl, W_1, [1, 1, 1, 1], padding='VALID') + b_1)
        self.h_conv2 = tf.nn.relu(tf.nn.conv2d(self.h_conv1, W_2, [1, 1, 1, 1], padding='SAME') + b_2)
        self.h_conv21 = tf.nn.relu(tf.nn.conv2d(self.h_conv2, W_21, [1, 1, 1, 1], padding='SAME') + b_21)

        shape = self.h_conv21.get_shape().as_list()
        dim = np.cumprod(shape[1:])[-1]
        h_conv_out = tf.reshape(self.h_conv21, [-1, dim])

        num_hidden = 128
        W_3 = self.weight_variable([dim, num_hidden])
        b_3 = self.bias_variable([num_hidden])
        W_4 = self.weight_variable([num_hidden, Pre.NUM_ACTIONS])
        b_4 = self.bias_variable([Pre.NUM_ACTIONS])

        self.hidden = tf.matmul(h_conv_out, W_3) + b_3
        predictions = tf.matmul(self.hidden, W_4) + b_4

        self.cross_entropy = tf.nn.softmax_cross_entropy_with_logits(predictions, actions_pl)
        self.loss = tf.reduce_mean(self.cross_entropy)
        print("states_pl shape:", states_pl.get_shape())
        print("actions_pl shape:", actions_pl.get_shape())
        print("predictions shape:", predictions.get_shape())
        print("cross_entropy shape:", self.cross_entropy.get_shape())
        print("loss shape:", self.loss.get_shape())

        tf.scalar_summary("loss", self.loss)
        self.optimizer = tf.train.AdamOptimizer()
        self.opt_op = self.optimizer.minimize(self.loss)

        self.predict_probs = tf.nn.softmax(predictions)
        eq = tf.equal(tf.argmax(self.predict_probs, 1), tf.argmax(actions_pl, 1))
        self.eval_correct = tf.reduce_sum(tf.cast(eq, tf.int32))

        self.rl_op(actions_pl)

    def rl_op(self, actions_pl):
        self.rewards_pl = tf.placeholder(tf.float32, shape=[None])

        # SARSA: alpha * [r + gamma * Q(s', a') - Q(s, a)] * grad
        # Q: alpha * [r + gamma * max<a>Q(s', a) − Q(s, a)] * grad

        print("rewards_pl shape:", self.rewards_pl.get_shape())

        maxa = tf.reduce_max(self.predict_probs, reduction_indices=1)
        qsa = tf.boolean_mask(self.predict_probs, tf.cast(actions_pl, tf.bool))
        delta = self.rewards_pl + 0.9 * maxa - qsa
        print('delta shape:', delta.get_shape())
        delta = tf.reduce_mean(delta)

        gradients = self.optimizer.compute_gradients(self.loss)
        print("gradients size:", len(gradients))
        for i, (grad, var) in enumerate(gradients):
            tf.histogram_summary(var.name, var)
            if grad is not None:
                tf.histogram_summary(var.name + '/gradients', grad)
                gradients[i] = (0.1 * grad * delta, var)

        self.train_op = self.optimizer.apply_gradients(gradients)

#         loss = tf.reduce_mean(0.01 * self.cross_entropy * self.rewards_pl)
#         self.train_op = self.optimizer.minimize(loss)


    def forge(self, row):
        board = row[:Board.BOARD_SIZE_SQ]
        image, _ = self.adapt_state(board)

        visit = row[Board.BOARD_SIZE_SQ::2]
#         visit[visit == 0] = 1
#         win = row[Board.BOARD_SIZE_SQ+1::2]
        win_rate = visit
        s = np.sum(win_rate)
        win_rate /= s
        return image, win_rate

    def adapt(self, filename):
        proc = psutil.Process(os.getpid())
        gc.collect()
        mem0 = proc.memory_info().rss

        if self.ds_train is not None and not self.loader_train.is_wane:
            self.ds_train = None
        if self.ds_valid is not None and not self.loader_valid.is_wane:
            self.ds_valid = None
        if self.ds_test is not None and not self.loader_test.is_wane:
            self.ds_test = None

        gc.collect()

        mem1 = proc.memory_info().rss
        print('gc(M):', (mem1 - mem0) / 1024 ** 2)

        h, w, c = self.get_input_shape()

        def f(dat):
            ds = []
            for row in dat:
                s, a = self.forge(row)
                ds.append((s, a))
            ds = np.array(ds)
            return DataSet(np.vstack(ds[:, 0]).reshape((-1, h, w, c)), np.vstack(ds[:, 1]))

        if self.ds_train is None:
            ds_train, self._has_more_data = self.loader_train.load(Pre.DATASET_CAPACITY)
            self.ds_train = f(ds_train)
        if self.ds_valid is None:
            ds_valid, _ = self.loader_valid.load(Pre.DATASET_CAPACITY // 2)
            self.ds_valid = f(ds_valid)
        if self.ds_test is None:
            ds_test, _ = self.loader_test.load(Pre.DATASET_CAPACITY // 2)
            self.ds_test = f(ds_test)

        print(self.ds_train.images.shape, self.ds_train.labels.shape)
        print(self.ds_valid.images.shape, self.ds_valid.labels.shape)
        print(self.ds_test.images.shape, self.ds_test.labels.shape)


    def adapt_state(self, board):
        board = board.reshape(-1, Board.BOARD_SIZE)
        board = self.regulate(board)
        return super(DCNN2, self).adapt_state(board)

    def get_input_shape(self):
        assert Board.BOARD_SIZE >= 5
        height = 6 * Board.BOARD_SIZE - 18  # row vecs + col vecs + valid(len>=5) main diag vecs + valid(len>=5) anti diag vecs
        return height, Board.BOARD_SIZE, Pre.NUM_CHANNELS

    def mid_vis(self, feed_dict):
        pass

    def swallow(self, who, st0, st1, **kwargs):
        self.observation.append((who, st0, st1))

    def absorb(self, winner, **kwargs):
        h, w, c = self.get_input_shape()

        states = []
        actions = []
        rewards = []

        for who, st0, st1 in self.observation:
            reward = 0
            if winner != 0:
                reward = 1 if who == winner else -1
            action = np.not_equal(st1.stones, st0.stones).astype(np.float32)
            state, _ = self.adapt_state(st1.stones)
            state = state.reshape((-1, h, w, c))
            states.append(state)
            actions.append(action)
            rewards.append(reward)

        states = np.vstack(states)
        actions = np.vstack(actions)
        rewards = np.array(rewards)

#         print('reinforce T:', rewards.shape[0], ', R:', rewards[0])

#         print("ds_rl states shape:", states.shape)
#         print("ds_rl actions shape:", actions.shape)
#         print("ds_rl rewards shape:", rewards.shape)
        self.sess.run(self.train_op, feed_dict={self.states_pl:states, self.actions_pl:actions, self.rewards_pl:rewards})
        self.gstep += 1
#         self.ds_rl.clear()

    def void(self):
        self.observation = []

if __name__ == '__main__':
    n = DCNN2(is_revive=False)
    n.deploy()
    n.run()



