from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.autograd import Variable

from pysc2.lib import actions
from pysc2.lib import features


class A2CAgent(object):

    def __init__(self,
                 dims, 
                 in_channel_screen,
                 in_channel_minimap,
                 rmsprop_lr=1e-4,
                 rmsprop_eps=1e-8,
                 rollout_num_steps=5,
                 discount=0.99,
                 ent_coef=1e-3,
                 val_coef=1.0,
                 use_gpu=True,
                 seed=0):
        torch.manual_seed(seed)
        if use_gpu: torch.cuda.manual_seed(seed)

        self._actor_critic = FullyConvNet(dims, in_channel_screen,
                                          in_channel_minimap)
        if torch.cuda.device_count() > 1:
            self._actor_critic = nn.DataParallel(self._actor_critic)
        if use_gpu:
            self._actor_critic.cuda()
        self._optimizer = optim.RMSprop(self._actor_critic.parameters(),
                                        lr=rmsprop_lr,
                                        eps=rmsprop_eps,
                                        centered=False)

        self._rollout_num_steps = rollout_num_steps
        self._discount = discount
        self._ent_coef = ent_coef
        self._val_coef = val_coef
        self._use_gpu = use_gpu

    def step(self, ob):
        if isinstance(ob, tuple):
            ob = tuple(np.expand_dims(ob, 0) for o in ob)
        else:
            ob = np.expand_dims(ob, 0)
        ob = self._ndarray_to_tensor(ob)
        prob_logit, _ = self._actor_critic(
            tuple(Variable(tensor, volatile=True) for tensor in ob))
        action = self._sample_action(prob_logit.data)
        return action.numpy() if not self._use_gpu else action.cpu().numpy()

    def train(self, envs):
        obs = envs.reset()
        while True:
            obs_mb, action_mb, target_value_mb, obs = self._rollout(envs, obs)
            self._update(obs_mb, action_mb, target_value_mb)

    def _rollout(self, envs, obs):
        obs_mb, action_mb, reward_mb, done_mb = [], [], [], []
        for _ in xrange(self._rollout_num_steps):
            obs = self._ndarray_to_tensor(obs)
            prob_logit, _ = self._actor_critic(
                tuple(Variable(tensor, volatile=True) for tensor in obs))
            action = self._sample_action(prob_logit.data)
            obs_mb.append(obs)
            action_mb.append(action)
            obs, reward, done, _ = envs.step(action.numpy() if not self._use_gpu
                                             else action.cpu().numpy())
            reward_mb.append(torch.Tensor(reward) if not self._use_gpu
                             else torch.cuda.FloatTensor(reward))
            done_mb.append(torch.Tensor(done.tolist()) if not self._use_gpu
                           else torch.cuda.FloatTensor(done.tolist()))
        target_value_mb = self._boostrap(reward_mb, done_mb, obs)
        return obs_mb, action_mb, target_value_mb,  obs

    def _update(self, obs_mb, action_mb, target_value_mb):
        prob_logit, value = self._actor_critic(
            tuple(Variable(torch.cat([obs[c] for obs in obs_mb])) 
                  for c in xrange(len(obs_mb[0]))))
        log_prob = F.log_softmax(prob_logit, 1)
        prob = F.softmax(prob_logit, 1)
        entropy = -(log_prob * prob).sum(1)

        advantage = Variable(torch.cat(target_value_mb)) - value
        action = Variable(torch.cat(action_mb))
        value_loss = advantage.pow(2).mean() * 0.5
        policy_loss = - (log_prob.gather(1, action) *
                         Variable(advantage.data)).mean()
        entropy_loss = entropy.mean()
        loss = policy_loss + self._val_coef * value_loss + \
               self._ent_coef * entropy_loss

        self._optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm(self._actor_critic.parameters(), 40)
        self._optimizer.step()

    def _boostrap(self, reward_mb, done_mb, last_obs):
        last_obs = self._ndarray_to_tensor(last_obs)
        _, last_value = self._actor_critic(
            tuple(Variable(tensor, volatile=True) for tensor in last_obs))
        target_value = []
        r = last_value.data.squeeze() * (1 - done_mb[-1])
        for reward, done in reversed(zip(reward_mb, done_mb)):
            r *= 1 - done 
            r = self._discount * r + reward
            target_value.append(r.unsqueeze(1))
        return target_value[::-1]

    def _ndarray_to_tensor(self, arrays):
        if isinstance(arrays, tuple):
            if self._use_gpu:
                return [torch.from_numpy(array).cuda() for array in arrays]
            else:
                return [torch.from_numpy(o) for array in arrays]
        else:
            if self._use_gpu:
                return torch.from_numpy(arrays).cuda()
            else:
                return torch.from_numpy(arrays)


    def _sample_action(self, logit):
        return F.softmax(Variable(logit), 1).multinomial(1).data
            
class FullyConvNet(nn.Module):
    def __init__(self, dims, in_channels_screen, in_channels_minimap):
        super(FullyConvNet, self).__init__()
        self.screen_conv1 = nn.Conv2d(in_channels=in_channels_screen,
                                      out_channels=16,
                                      kernel_size=5,
                                      stride=1,
                                      padding=2)
        self.screen_conv2 = nn.Conv2d(in_channels=16,
                                      out_channels=32,
                                      kernel_size=3,
                                      stride=1,
                                      padding=1)
        self.minimap_conv1 = nn.Conv2d(in_channels=in_channels_minimap,
                                       out_channels=16,
                                       kernel_size=5,
                                       stride=1,
                                       padding=2)
        self.minimap_conv2 = nn.Conv2d(in_channels=16,
                                       out_channels=32,
                                       kernel_size=3,
                                       stride=1,
                                       padding=1)
        self.policy_conv = nn.Conv2d(in_channels=64,
                                     out_channels=1,
                                     kernel_size=1,
                                     stride=1,
                                     padding=0)
        self.fc = nn.Linear(64 * dims * dims, 64)
        self.value_fc = nn.Linear(64, 1)

    def forward(self, x):
        screen_x, minimap_x = x
        screen_x = F.relu(self.screen_conv1(screen_x))
        screen_x = F.relu(self.screen_conv2(screen_x))
        minimap_x = F.relu(self.minimap_conv1(minimap_x))
        minimap_x = F.relu(self.minimap_conv2(minimap_x))
        x = torch.cat((screen_x, minimap_x), 1)
        v = F.relu(self.fc(x.view(x.size(0), -1)))
        x = self.policy_conv(x)
        policy = x.view(x.size(0), -1) * 3
        value = self.value_fc(v)
        return policy, value
