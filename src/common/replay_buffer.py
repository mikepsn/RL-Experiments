"""
This file is adapted from following files in openai/baselines.
common/segment_tree.py
deepq/replay_buffer.py
baselines/acer/buffer.py
"""
import operator
import random

import numpy as np
import torch


class SegmentTree(object):
    def __init__(self, capacity, operation, neutral_element):
        """Build a Segment Tree data structure.

        https://en.wikipedia.org/wiki/Segment_tree

        Can be used as regular array, but with two
        important differences:

            a) setting item's value is slightly slower.
               It is O(lg capacity) instead of O(1).
            b) user has access to an efficient ( O(log segment size) )
               `reduce` operation which reduces `operation` over
               a contiguous subsequence of items in the array.

        Paramters
        ---------
        capacity: int
            Total size of the array - must be a power of two.
        operation: lambda obj, obj -> obj
            and operation for combining elements (eg. sum, max)
            must form a mathematical group together with the set of
            possible values for array elements (i.e. be associative)
        neutral_element: obj
            neutral element for the operation above. eg. float('-inf')
            for max and 0 for sum.
        """
        assert capacity > 0 and capacity & (capacity - 1) == 0, \
            "capacity must be positive and a power of 2."
        self._capacity = capacity
        self._value = [neutral_element for _ in range(2 * capacity)]
        self._operation = operation

    def _reduce_helper(self, start, end, node, node_start, node_end):
        if start == node_start and end == node_end:
            return self._value[node]
        mid = (node_start + node_end) // 2
        if end <= mid:
            return self._reduce_helper(start, end, 2 * node, node_start, mid)
        else:
            if mid + 1 <= start:
                return self._reduce_helper(start, end,
                                           2 * node + 1, mid + 1, node_end)
            else:
                return self._operation(
                    self._reduce_helper(start, mid,
                                        2 * node, node_start, mid),
                    self._reduce_helper(mid + 1, end,
                                        2 * node + 1, mid + 1, node_end)
                )

    def reduce(self, start=0, end=None):
        """Returns result of applying `self.operation`
        to a contiguous subsequence of the array.

        Parameters
        ----------
        start: int
            beginning of the subsequence
        end: int
            end of the subsequences

        Returns
        -------
        reduced: obj
            result of reducing self.operation over the specified range of array.
        """
        if end is None:
            end = self._capacity
        if end < 0:
            end += self._capacity
        end -= 1
        return self._reduce_helper(start, end, 1, 0, self._capacity - 1)

    def __setitem__(self, idx, val):
        # index of the leaf
        idx += self._capacity
        self._value[idx] = val
        idx //= 2
        while idx >= 1:
            self._value[idx] = self._operation(
                self._value[2 * idx],
                self._value[2 * idx + 1]
            )
            idx //= 2

    def __getitem__(self, idx):
        assert 0 <= idx < self._capacity
        return self._value[self._capacity + idx]


class SumSegmentTree(SegmentTree):
    def __init__(self, capacity):
        super(SumSegmentTree, self).__init__(
            capacity=capacity,
            operation=operator.add,
            neutral_element=0.0
        )

    def sum(self, start=0, end=None):
        """Returns arr[start] + ... + arr[end]"""
        return super(SumSegmentTree, self).reduce(start, end)

    def find_prefixsum_idx(self, prefixsum):
        """Find the highest index `i` in the array such that
            sum(arr[0] + arr[1] + ... + arr[i - i]) <= prefixsum

        if array values are probabilities, this function
        allows to sample indexes according to the discrete
        probability efficiently.

        Parameters
        ----------
        perfixsum: float
            upperbound on the sum of array prefix

        Returns
        -------
        idx: int
            highest index satisfying the prefixsum constraint
        """
        assert 0 <= prefixsum <= self.sum() + 1e-5
        idx = 1
        while idx < self._capacity:  # while non-leaf
            if self._value[2 * idx] > prefixsum:
                idx = 2 * idx
            else:
                prefixsum -= self._value[2 * idx]
                idx = 2 * idx + 1
        return idx - self._capacity


class MinSegmentTree(SegmentTree):
    def __init__(self, capacity):
        super(MinSegmentTree, self).__init__(
            capacity=capacity,
            operation=min,
            neutral_element=float('inf')
        )

    def min(self, start=0, end=None):
        """Returns min(arr[start], ...,  arr[end])"""

        return super(MinSegmentTree, self).reduce(start, end)


class ReplayBuffer(object):
    def __init__(self, size, device):
        """Create Replay buffer.

        Parameters
        ----------
        size: int
            Max number of transitions to store in the buffer. When the buffer
            overflows the old memories are dropped.
        """
        self._storage = []
        self._maxsize = size
        self._next_idx = 0
        self._device = device

    def __len__(self):
        return len(self._storage)

    def add(self, o, a, r, o_, d):
        data = (o, a, r, o_, d)
        if self._next_idx >= len(self._storage):
            self._storage.append(data)
        else:
            self._storage[self._next_idx] = data
        self._next_idx = (self._next_idx + 1) % self._maxsize

    def _encode_sample(self, idxes):
        b_o, b_a, b_r, b_o_, b_d = [], [], [], [], []
        for i in idxes:
            o, a, r, o_, d = self._storage[i]
            b_o.append(o.astype('float32'))
            b_a.append([a])
            b_r.append([r])
            b_o_.append(o_.astype('float32'))
            b_d.append([int(d)])
        res = (
            torch.from_numpy(np.asarray(b_o)).to(self._device),
            torch.from_numpy(np.asarray(b_a)).to(self._device).long(),
            torch.from_numpy(np.asarray(b_r)).to(self._device).float(),
            torch.from_numpy(np.asarray(b_o_)).to(self._device),
            torch.from_numpy(np.asarray(b_d)).to(self._device).float(),
        )
        return res

    def sample(self, batch_size):
        """Sample a batch of experiences."""
        indexes = range(len(self._storage))
        idxes = [random.choice(indexes) for _ in range(batch_size)]
        return self._encode_sample(idxes)


class PrioritizedReplayBuffer(ReplayBuffer):
    def __init__(self, size, device, alpha, beta):
        """Create Prioritized Replay buffer.

        Parameters
        ----------
        size: int
            Max number of transitions to store in the buffer. When the buffer
            overflows the old memories are dropped.
        alpha: float
            how much prioritization is used
            (0 - no prioritization, 1 - full prioritization)

        See Also
        --------
        ReplayBuffer.__init__
        """
        super(PrioritizedReplayBuffer, self).__init__(size, device)
        assert alpha >= 0
        self._alpha = alpha

        it_capacity = 1
        while it_capacity < size:
            it_capacity *= 2

        self._it_sum = SumSegmentTree(it_capacity)
        self._it_min = MinSegmentTree(it_capacity)
        self._max_priority = 1.0
        self.beta = beta

    def add(self, *args, **kwargs):
        """See ReplayBuffer.store_effect"""
        idx = self._next_idx
        super().add(*args, **kwargs)
        self._it_sum[idx] = self._max_priority ** self._alpha
        self._it_min[idx] = self._max_priority ** self._alpha

    def _sample_proportional(self, batch_size):
        res = []
        p_total = self._it_sum.sum(0, len(self._storage) - 1)
        every_range_len = p_total / batch_size
        for i in range(batch_size):
            mass = random.random() * every_range_len + i * every_range_len
            idx = self._it_sum.find_prefixsum_idx(mass)
            res.append(idx)
        return res

    def sample(self, batch_size):
        """Sample a batch of experiences"""
        idxes = self._sample_proportional(batch_size)

        it_sum = self._it_sum.sum()
        p_min = self._it_min.min() / it_sum
        max_weight = (p_min * len(self._storage)) ** (-self.beta)

        p_samples = np.asarray([self._it_sum[idx] for idx in idxes]) / it_sum
        weights = (p_samples * len(self._storage)) ** (-self.beta) / max_weight
        weights = torch.from_numpy(weights.astype('float32'))
        weights = weights.to(self._device).unsqueeze(1)
        encoded_sample = self._encode_sample(idxes)
        return encoded_sample + (weights, idxes)

    def update_priorities(self, idxes, priorities):
        """Update priorities of sampled transitions"""
        assert len(idxes) == len(priorities)
        for idx, priority in zip(idxes, priorities):
            assert priority > 0
            assert 0 <= idx < len(self._storage)
            self._it_sum[idx] = priority ** self._alpha
            self._it_min[idx] = priority ** self._alpha

            self._max_priority = max(self._max_priority, priority)


class VecReplayBuffer(object):
    """Replay buffer used in ACER. Parrallel envs and multi-step are considered.
    Note that this buffer is different with replay buffer used in DQN.
    """
    def __init__(self, env, nenv, nsteps, size, device):
        self.nsteps = nsteps
        self.o_shape = env.observation_space.shape
        self.o_dtype = env.observation_space.dtype
        self.a_num = env.action_space.n
        self.a_dtype = env.action_space.dtype
        self.nenv = nenv
        self.nstack = env.k
        self.nc = self.o_shape[0] // self.nstack
        self.nbatch = self.nenv * self.nsteps
        # Each loc contains nenv * nsteps frames
        self.size = size // self.nsteps

        # Memory
        s = (self.nsteps + self.nstack, self.nc) + self.o_shape[1:]
        self.enc_o = np.empty((self.size, self.nenv) + s, dtype=self.o_dtype)
        s = (self.size, self.nenv, self.nsteps)
        self.a = np.empty(s, dtype=self.a_dtype)
        self.r = np.empty(s, dtype=np.float32)
        self.p = np.empty(s + (self.a_num, ), dtype=np.float32)
        self.done = np.empty(s, dtype=np.int32)

        # Size indexes
        self.next_idx = 0
        self.nonempty_num = 0

        self.device = device

    def add(self, enc_o, a, r, p, done):
        """Add sample
        Args:
            enc_o (numpy.array): shape (nenv, nstack + nstep, nc, nh, nw)
            a (numpy.array): shape (nenv, nsteps)
            r (numpy.array): shape (nenv, nsteps)
            p (numpy.array): shape (nenv, nsteps, nacts)
            done (numpy.array): shape (nenv, nsteps)
        """
        self.enc_o[self.next_idx] = enc_o
        self.a[self.next_idx] = a
        self.r[self.next_idx] = r
        self.p[self.next_idx] = p
        self.done[self.next_idx] = done
        self.next_idx = (self.next_idx + 1) % self.size
        self.nonempty_num = min(self.size, self.nonempty_num + 1)

    def _take(self, x, idx):
        out = np.empty((self.nenv, ) + x.shape[2:], dtype=x.dtype)
        for i in range(self.nenv):
            out[i] = x[idx[i], i]
        return out

    def sample(self):
        """Get a sample per env. Across envs will lead higher correlation.
        Returns:
            o (numpy.array): shape (nenv, 1 + nstep, nstack * nc, nh, nw)
            a (numpy.array): shape (nenv, nsteps)
            r (numpy.array): shape (nenv, nsteps)
            p (numpy.array): shape (nenv, nsteps, nacts)
            done (numpy.array): shape (nenv, nsteps)
        """
        idx = np.random.randint(0, self.nonempty_num, self.nenv)
        b_done = self._take(self.done, idx)
        b_o = self._stack_obs(self._take(self.enc_o, idx), b_done)
        b_a = self._take(self.a, idx)
        b_r = self._take(self.r, idx)
        b_p = self._take(self.p, idx)
        return (
            torch.from_numpy(b_o).to(self.device).float(),
            torch.from_numpy(b_a).to(self.device).long(),
            torch.from_numpy(b_r).to(self.device).float(),
            torch.from_numpy(b_p).to(self.device).float(),
            torch.from_numpy(b_done).to(self.device).float()
        )

    def _stack_obs(self, enc_obs, dones):
        """Stack obseverations
        Args:
            enc_obs (numpy.array): shape (nenv, nstack + nstep, nc, nh, nw)
            dones (numpy.array): shape (nenv, nsteps)
        Returns:
            obs (numpy.array): shape (nenv, nstep + 1, nc * nstack, nh, nw)
        """
        returnob_shape = (self.nenv, self.nsteps + 1,
                          self.nstack * self.nc) + self.o_shape[1:]

        obs = np.zeros(returnob_shape, dtype=enc_obs.dtype)
        mask = np.ones((self.nenv, self.nsteps + 1), dtype=enc_obs.dtype)
        mask[:, 1:] = 1.0 - dones
        mask = mask.reshape(mask.shape + (1, 1, 1))

        for i in range(self.nstack - 1, -1, -1):
            obs[:, :, i*self.nc:(i+1)*self.nc] = enc_obs[:, i:i+self.nsteps+1]
            if i < self.nstack - 1:
                obs[:, :, i*self.nc:(i+1)*self.nc] *= mask
                mask[:, 1:] *= mask[:, :-1]

        return obs
