import numpy as np
from multiprocessing import Process, Pipe

import gym


def worker(pipe, env_create_func):
    env = env_create_func()
    while True:
        cmd, data = pipe.recv()
        if cmd == 'step':
            ob, reward, done, info = env.step(data)
            if done: ob = env.reset()
            pipe.send((ob, reward, done, info))
        elif cmd == 'reset':
            ob = env.reset()
            pipe.send(ob)
        elif cmd == 'close':
            env.close()
            break
        elif cmd == 'get_spaces':
            pipe.send((env.action_space, env.observation_space))
        else:
            raise NotImplementedError


class ParallelEnvWrapper(gym.Env):
    def __init__(self, env_create_funcs):
        self._pipes, self._pipes_remote = zip(
            *[Pipe() for _ in range(len(env_create_funcs))])
        self._processes = [Process(target=worker, args=(pipe, env_fn,))
                           for (pipe, env_fn) in zip(self._pipes_remote,
                                                     env_create_funcs)]
        for p in self._processes:
            p.daemon = True
            p.start()

    def _step(self, actions):
        for pipe, action in zip(self._pipes, actions):
            pipe.send(('step', action))
        results = [pipe.recv() for pipe in self._pipes]
        obs, rewards, dones, infos = zip(*results)
        if isinstance(obs[0], tuple):
            n = len(obs[0])
            obs = tuple(np.stack([ob[c] for ob in obs]) for c in xrange(n))
        else:
            obs = np.stack(obs)
        return obs, np.stack(rewards), np.stack(dones), infos

    def _reset(self):
        for pipe in self._pipes:
            pipe.send(('reset', None))
        obs = [pipe.recv() for pipe in self._pipes]
        if isinstance(obs[0], tuple):
            n = len(obs[0])
            obs = tuple(np.stack([ob[c] for ob in obs]) for c in xrange(n))
        else:
            obs = np.stack(obs)
        return obs

    def _close(self):
        for pipe in self._pipes:
            pipe.send(('close', None))
        for p in self._processes:
            p.join()

    @property
    def num_envs(self):
        return len(self._pipes)
