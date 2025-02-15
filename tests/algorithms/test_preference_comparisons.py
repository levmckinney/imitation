"""Tests for the preference comparisons reward learning implementation."""

import re
from typing import Sequence
from unittest import mock

import numpy as np
import pytest
import seals  # noqa: F401
import stable_baselines3
import torch as th
from stable_baselines3.common.envs import FakeImageEnv
from stable_baselines3.common.vec_env import DummyVecEnv

import imitation.testing.reward_nets as testing_reward_nets
from imitation.algorithms import preference_comparisons
from imitation.data import types
from imitation.data.types import TrajectoryWithRew
from imitation.rewards import reward_nets
from imitation.util import networks, util

UNCERTAINTY_ON = ["logit", "probability", "label"]


@pytest.fixture
def venv():
    return util.make_vec_env(
        "seals/CartPole-v0",
        n_envs=1,
    )


@pytest.fixture(
    params=[
        reward_nets.BasicRewardNet,
        testing_reward_nets.make_ensemble,
        lambda *args: reward_nets.AddSTDRewardWrapper(
            testing_reward_nets.make_ensemble(*args),
        ),
    ],
)
def reward_net(request, venv):
    return request.param(venv.observation_space, venv.action_space)


@pytest.fixture
def agent(venv):
    return stable_baselines3.PPO(
        "MlpPolicy",
        venv,
        n_epochs=1,
        batch_size=2,
        n_steps=10,
    )


@pytest.fixture
def random_fragmenter():
    return preference_comparisons.RandomFragmenter(seed=0, warning_threshold=0)


@pytest.fixture
def trajectory_with_reward() -> TrajectoryWithRew:
    return TrajectoryWithRew(
        obs=np.zeros((33, 10), dtype=float),
        acts=np.zeros(32, dtype=int),
        infos=None,
        terminal=False,
        rews=np.zeros(32, dtype=float),
    )


@pytest.fixture
def agent_trainer(agent, reward_net, venv):
    return preference_comparisons.AgentTrainer(agent, reward_net, venv)


def _check_trajs_equal(
    trajs1: Sequence[types.TrajectoryWithRew],
    trajs2: Sequence[types.TrajectoryWithRew],
):
    assert len(trajs1) == len(trajs2)
    for traj1, traj2 in zip(trajs1, trajs2):
        assert np.array_equal(traj1.obs, traj2.obs)
        assert np.array_equal(traj1.acts, traj2.acts)
        assert np.array_equal(traj1.rews, traj2.rews)
        assert np.array_equal(traj1.infos, traj2.infos)
        assert traj1.terminal == traj2.terminal


def test_mismatched_spaces(venv, agent):
    other_venv = util.make_vec_env(
        "seals/MountainCar-v0",
        n_envs=1,
    )
    bad_reward_net = reward_nets.BasicRewardNet(
        other_venv.observation_space,
        other_venv.action_space,
    )
    with pytest.raises(
        ValueError,
        match="spaces do not match",
    ):
        preference_comparisons.AgentTrainer(agent, bad_reward_net, venv)


def test_trajectory_dataset_seeding(
    cartpole_expert_trajectories: Sequence[TrajectoryWithRew],
    num_samples: int = 400,
):
    dataset1 = preference_comparisons.TrajectoryDataset(
        cartpole_expert_trajectories,
        seed=0,
    )
    sample1 = dataset1.sample(num_samples)
    dataset2 = preference_comparisons.TrajectoryDataset(
        cartpole_expert_trajectories,
        seed=0,
    )
    sample2 = dataset2.sample(num_samples)

    _check_trajs_equal(sample1, sample2)

    dataset3 = preference_comparisons.TrajectoryDataset(
        cartpole_expert_trajectories,
        seed=42,
    )
    sample3 = dataset3.sample(num_samples)
    with pytest.raises(AssertionError):
        _check_trajs_equal(sample2, sample3)


# CartPole max episode length is 200
@pytest.mark.parametrize("num_steps", [0, 199, 200, 201, 400])
def test_trajectory_dataset_len(
    cartpole_expert_trajectories: Sequence[TrajectoryWithRew],
    num_steps: int,
):
    dataset = preference_comparisons.TrajectoryDataset(
        cartpole_expert_trajectories,
        seed=0,
    )
    sample = dataset.sample(num_steps)
    lengths = [len(t) for t in sample]
    assert sum(lengths) >= num_steps
    if num_steps > 0:
        assert sum(lengths) - min(lengths) < num_steps


def test_trajectory_dataset_too_long(
    cartpole_expert_trajectories: Sequence[TrajectoryWithRew],
):
    dataset = preference_comparisons.TrajectoryDataset(
        cartpole_expert_trajectories,
        seed=0,
    )
    with pytest.raises(RuntimeError, match="Asked for.*but only.* available"):
        dataset.sample(100000)


def test_trajectory_dataset_shuffle(
    cartpole_expert_trajectories: Sequence[TrajectoryWithRew],
    num_steps: int = 400,
):
    dataset = preference_comparisons.TrajectoryDataset(
        cartpole_expert_trajectories,
        seed=0,
    )
    sample = dataset.sample(num_steps)
    sample2 = dataset.sample(num_steps)
    with pytest.raises(AssertionError):
        _check_trajs_equal(sample, sample2)


def test_transitions_left_in_buffer(agent_trainer):
    # Faster to just set the counter than to actually fill the buffer
    # with transitions.
    agent_trainer.buffering_wrapper.n_transitions = 2
    with pytest.raises(
        RuntimeError,
        match=re.escape(
            "There are 2 transitions left in the buffer. "
            "Call AgentTrainer.sample() first to clear them.",
        ),
    ):
        agent_trainer.train(steps=1)


def test_mixture_of_trajectory_generators_train_and_sample(trajectory_with_reward):
    gen_1 = mock.Mock(spec=preference_comparisons.TrajectoryGenerator)
    gen_2 = mock.Mock(spec=preference_comparisons.TrajectoryGenerator)
    gen_1.sample.return_value = 6 * [trajectory_with_reward]
    gen_2.sample.return_value = 6 * [trajectory_with_reward]
    mixture = preference_comparisons.MixtureOfTrajectoryGenerators(
        members=(gen_1, gen_2),
        share_training_steps=False,
    )
    training_steps = 11
    num_generators = 2
    kwargs = dict(foo=4)
    mixture.train(steps=training_steps, **kwargs)
    # When share training steps is off we expect each
    # generator to train for the full 10 step
    assert gen_1.train.called_once_with(steps=training_steps, **kwargs)
    assert gen_2.train.called_once_with(steps=training_steps, **kwargs)
    # When `share_training_step`s is enabled we expect the training steps to be split
    # close to equally amongst the generators
    mixture.share_training_steps = True
    mixture.train(training_steps)
    assert (
        gen_1.train.call_args.args[0] + gen_2.train.call_args.args[0] == training_steps
    )
    assert gen_1.train.call_args.args[0] >= training_steps // num_generators
    assert gen_2.train.call_args.args[0] >= training_steps // num_generators
    num_samples = 11
    # Similarly, we expect the samples to always be split evenly amongst the
    # generators.
    mixture.sample(training_steps)
    assert (
        gen_1.sample.call_args.args[0] + gen_2.sample.call_args.args[0] == num_samples
    )
    assert gen_1.sample.call_args.args[0] >= num_samples // num_generators
    assert gen_2.sample.call_args.args[0] >= num_samples // num_generators


def test_mixture_of_trajectory_generators_raises_value_error_when_members_is_empty():
    with pytest.raises(ValueError):
        preference_comparisons.MixtureOfTrajectoryGenerators(
            members=[],
        )


@pytest.mark.parametrize(
    "schedule",
    ["constant", "hyperbolic", "inverse_quadratic", lambda t: 1 / (1 + t**3)],
)
def test_trainer_no_crash(
    agent_trainer,
    reward_net,
    random_fragmenter,
    custom_logger,
    schedule,
):
    main_trainer = preference_comparisons.PreferenceComparisons(
        agent_trainer,
        reward_net,
        num_iterations=2,
        transition_oversampling=2,
        fragment_length=2,
        fragmenter=random_fragmenter,
        custom_logger=custom_logger,
        query_schedule=schedule,
    )
    result = main_trainer.train(100, 10)
    # We don't expect good performance after training for 10 (!) timesteps,
    # but check stats are within the bounds they should lie in.
    assert result["reward_loss"] > 0.0
    assert 0.0 < result["reward_accuracy"] <= 1.0


def test_reward_ensemble_trainer_raises_type_error(venv):
    reward_net = reward_nets.BasicRewardNet(venv.observation_space, venv.action_space)
    preference_model = preference_comparisons.PreferenceModel(
        model=reward_net,
        noise_prob=0.1,
        discount_factor=0.9,
        threshold=50,
    )
    loss = preference_comparisons.CrossEntropyRewardLoss(preference_model)

    with pytest.raises(
        TypeError,
        match=r"RewardEnsemble expected by EnsembleTrainer not .*",
    ):
        preference_comparisons.EnsembleTrainer(
            reward_net,
            loss,
        )


def test_correct_reward_trainer_used_by_default(
    agent_trainer,
    reward_net,
    random_fragmenter,
    custom_logger,
):
    main_trainer = preference_comparisons.PreferenceComparisons(
        agent_trainer,
        reward_net,
        num_iterations=2,
        transition_oversampling=2,
        fragment_length=2,
        fragmenter=random_fragmenter,
        custom_logger=custom_logger,
    )

    base_reward_net = reward_net.base if hasattr(reward_net, "base") else reward_net
    if isinstance(base_reward_net, reward_nets.RewardEnsemble):
        assert isinstance(
            main_trainer.reward_trainer,
            preference_comparisons.EnsembleTrainer,
        )
    else:
        assert isinstance(
            main_trainer.reward_trainer,
            preference_comparisons.BasicRewardTrainer,
        )


def test_init_raises_error_when_trying_use_improperly_wrapped_ensemble(
    agent_trainer,
    venv,
    random_fragmenter,
    custom_logger,
):
    reward_net = testing_reward_nets.make_ensemble(
        venv.observation_space,
        venv.action_space,
    )
    reward_net = reward_nets.NormalizedRewardNet(reward_net, networks.RunningNorm)
    rgx = (
        r"RewardEnsemble can only be wrapped by "
        r"AddSTDRewardWrapper but found NormalizedRewardNet."
    )
    with pytest.raises(
        ValueError,
        match=rgx,
    ):
        preference_comparisons.PreferenceComparisons(
            agent_trainer,
            reward_net,
            num_iterations=2,
            transition_oversampling=2,
            fragment_length=2,
            fragmenter=random_fragmenter,
            custom_logger=custom_logger,
        )


def test_discount_rate_no_crash(agent_trainer, venv, random_fragmenter, custom_logger):
    # also use a non-zero noise probability to check that doesn't cause errors
    reward_net = reward_nets.BasicRewardNet(venv.observation_space, venv.action_space)
    preference_model = preference_comparisons.PreferenceModel(
        model=reward_net,
        noise_prob=0.1,
        discount_factor=0.9,
        threshold=50,
    )
    loss = preference_comparisons.CrossEntropyRewardLoss(preference_model)
    reward_trainer = preference_comparisons.BasicRewardTrainer(
        reward_net,
        loss,
    )

    main_trainer = preference_comparisons.PreferenceComparisons(
        agent_trainer,
        reward_net,
        num_iterations=2,
        transition_oversampling=2,
        fragment_length=2,
        fragmenter=random_fragmenter,
        reward_trainer=reward_trainer,
        custom_logger=custom_logger,
    )
    main_trainer.train(100, 10)


def test_synthetic_gatherer_deterministic(agent_trainer, random_fragmenter):
    gatherer = preference_comparisons.SyntheticGatherer(temperature=0)
    trajectories = agent_trainer.sample(10)
    fragments = random_fragmenter(trajectories, fragment_length=2, num_pairs=2)
    preferences1 = gatherer(fragments)
    preferences2 = gatherer(fragments)
    assert np.all(preferences1 == preferences2)


def test_fragments_terminal(random_fragmenter):
    trajectories = [
        types.TrajectoryWithRew(
            obs=np.arange(4),
            acts=np.zeros((3,)),
            rews=np.zeros((3,)),
            infos=None,
            terminal=True,
        ),
        types.TrajectoryWithRew(
            obs=np.arange(3),
            acts=np.zeros((2,)),
            rews=np.zeros((2,)),
            infos=None,
            terminal=False,
        ),
    ]
    for _ in range(5):
        for frags in random_fragmenter(trajectories, fragment_length=2, num_pairs=2):
            for frag in frags:
                assert (frag.obs[-1] == 3) == frag.terminal


def test_fragments_too_short_error(agent_trainer):
    trajectories = agent_trainer.sample(2)
    random_fragmenter = preference_comparisons.RandomFragmenter(
        seed=0,
        warning_threshold=0,
    )
    with pytest.raises(
        ValueError,
        match="No trajectories are long enough for the desired fragment length.",
    ):
        # the only important bit is that fragment_length is higher than
        # we'll ever reach
        random_fragmenter(trajectories, fragment_length=10000, num_pairs=2)


def test_preference_dataset_errors(agent_trainer, random_fragmenter):
    dataset = preference_comparisons.PreferenceDataset()
    trajectories = agent_trainer.sample(2)
    fragments = random_fragmenter(trajectories, fragment_length=2, num_pairs=2)
    # just create something with a different shape:
    preferences = np.empty(len(fragments) + 1, dtype=np.float32)
    with pytest.raises(ValueError, match="Unexpected preferences shape"):
        dataset.push(fragments, preferences)

    # Now test dtype
    preferences = np.empty(len(fragments), dtype=np.float64)
    with pytest.raises(ValueError, match="preferences should have dtype float32"):
        dataset.push(fragments, preferences)


def test_preference_dataset_queue(agent_trainer, random_fragmenter):
    dataset = preference_comparisons.PreferenceDataset(max_size=5)
    trajectories = agent_trainer.sample(10)

    gatherer = preference_comparisons.SyntheticGatherer()
    for i in range(6):
        fragments = random_fragmenter(trajectories, fragment_length=2, num_pairs=1)
        preferences = gatherer(fragments)
        assert len(dataset) == min(i, 5)
        dataset.push(fragments, preferences)
        assert len(dataset) == min(i + 1, 5)

    # The first comparison should have been evicted to keep the size at 5
    assert len(dataset) == 5


def test_store_and_load_preference_dataset(agent_trainer, random_fragmenter, tmp_path):
    dataset = preference_comparisons.PreferenceDataset()
    trajectories = agent_trainer.sample(10)
    fragments = random_fragmenter(trajectories, fragment_length=2, num_pairs=2)
    gatherer = preference_comparisons.SyntheticGatherer()
    preferences = gatherer(fragments)
    dataset.push(fragments, preferences)

    path = tmp_path / "preferences.pkl"
    dataset.save(path)
    loaded = preference_comparisons.PreferenceDataset.load(path)
    assert len(loaded) == len(dataset)
    for sample, loaded_sample in zip(dataset, loaded):
        fragments, preference = sample
        loaded_fragments, loaded_preference = loaded_sample

        assert preference == loaded_preference
        _check_trajs_equal(fragments, loaded_fragments)


def test_exploration_no_crash(
    agent,
    reward_net,
    venv,
    random_fragmenter,
    custom_logger,
):
    agent_trainer = preference_comparisons.AgentTrainer(
        agent,
        reward_net,
        venv,
        exploration_frac=0.5,
    )
    main_trainer = preference_comparisons.PreferenceComparisons(
        agent_trainer,
        reward_net,
        num_iterations=2,
        transition_oversampling=2,
        fragment_length=5,
        fragmenter=random_fragmenter,
        custom_logger=custom_logger,
    )
    main_trainer.train(100, 10)


@pytest.mark.parametrize("uncertainty_on", UNCERTAINTY_ON)
def test_active_fragmenter_discount_rate_no_crash(
    agent_trainer,
    venv,
    random_fragmenter,
    uncertainty_on,
    custom_logger,
):
    # also use a non-zero noise probability to check that doesn't cause errors
    reward_net = reward_nets.RewardEnsemble(
        venv.observation_space,
        venv.action_space,
        members=[
            reward_nets.BasicRewardNet(venv.observation_space, venv.action_space)
            for _ in range(2)
        ],
    )
    preference_model = preference_comparisons.PreferenceModel(
        model=reward_net,
        noise_prob=0.1,
        discount_factor=0.9,
        threshold=50,
    )

    fragmenter = preference_comparisons.ActiveSelectionFragmenter(
        preference_model=preference_model,
        base_fragmenter=random_fragmenter,
        fragment_sample_factor=2,
        uncertainty_on=uncertainty_on,
        custom_logger=custom_logger,
    )

    preference_model = preference_comparisons.PreferenceModel(
        model=reward_net,
        noise_prob=0.1,
        discount_factor=0.9,
        threshold=50,
    )
    loss = preference_comparisons.CrossEntropyRewardLoss(preference_model)

    reward_trainer = preference_comparisons.EnsembleTrainer(
        reward_net,
        loss,
    )

    main_trainer = preference_comparisons.PreferenceComparisons(
        agent_trainer,
        reward_net,
        num_iterations=2,
        transition_oversampling=2,
        fragment_length=2,
        fragmenter=fragmenter,
        reward_trainer=reward_trainer,
        custom_logger=custom_logger,
    )
    main_trainer.train(100, 10)


@pytest.fixture
def ensemble_preference_model(venv) -> preference_comparisons.PreferenceComparisons:
    reward_net = reward_nets.RewardEnsemble(
        venv.observation_space,
        venv.action_space,
        members=[
            reward_nets.BasicRewardNet(venv.observation_space, venv.action_space)
            for _ in range(2)
        ],
    )
    return preference_comparisons.PreferenceModel(
        model=reward_net,
        noise_prob=0.1,
        discount_factor=0.9,
        threshold=50,
    )


@pytest.fixture
def preference_model(venv) -> preference_comparisons.PreferenceComparisons:
    reward_net = reward_nets.BasicRewardNet(venv.observation_space, venv.action_space)
    return preference_comparisons.PreferenceModel(
        model=reward_net,
        noise_prob=0.1,
        discount_factor=0.9,
        threshold=50,
    )


def test_probability_model_raises_error_when_ensemble_member_index_not_provided(
    ensemble_preference_model,
):
    assert ensemble_preference_model.is_ensemble
    with pytest.raises(
        ValueError,
        match="`ensemble_member_index` required for ensemble models",
    ):
        ensemble_preference_model([])


def test_active_fragmenter_uncertainty_on_not_supported_error(
    ensemble_preference_model,
    random_fragmenter,
):
    re_match = r".* not supported\.\n\s+`uncertainty_on` should be from .*"
    with pytest.raises(ValueError, match=re_match):
        preference_comparisons.ActiveSelectionFragmenter(
            preference_model=ensemble_preference_model,
            base_fragmenter=random_fragmenter,
            fragment_sample_factor=2,
            uncertainty_on="uncertainty_on",
        )

    with pytest.raises(ValueError, match=re_match):
        fragmenter = preference_comparisons.ActiveSelectionFragmenter(
            preference_model=ensemble_preference_model,
            base_fragmenter=random_fragmenter,
            fragment_sample_factor=2,
            uncertainty_on="logit",
        )
        fragmenter._uncertainty_on = "uncertainty_on"
        members = ensemble_preference_model.model.num_members
        fragmenter.variance_estimate(th.rand(10, members), th.rand(10, members))


def test_active_selection_raises_error_when_initialized_without_an_ensemble(
    preference_model,
    random_fragmenter,
):
    with pytest.raises(
        ValueError,
        match=r"Preference model not wrapped over an ensemble.*",
    ):
        preference_comparisons.ActiveSelectionFragmenter(
            preference_model=preference_model,
            base_fragmenter=random_fragmenter,
            fragment_sample_factor=2,
            uncertainty_on="logit",
        )


def test_agent_trainer_populates_buffer(agent_trainer):
    agent_trainer.train(steps=1)
    assert agent_trainer.buffering_wrapper.n_transitions > 0


def test_agent_trainer_sample(venv, agent_trainer):
    trajectories = agent_trainer.sample(2)
    assert len(trajectories) > 0
    assert all(
        trajectory.obs.shape[1:] == venv.observation_space.shape
        for trajectory in trajectories
    )


def test_agent_trainer_sample_image_observations():
    """Test `AgentTrainer.sample()` in an image environment.

    SB3 algorithms may rearrange the channel dimension in environments with image
    observations, but `sample()` should return observations matching the original
    environment.
    """
    venv = DummyVecEnv([lambda: FakeImageEnv()])
    reward_net = reward_nets.BasicRewardNet(venv.observation_space, venv.action_space)
    agent = stable_baselines3.PPO(
        "MlpPolicy",
        venv,
        n_epochs=1,
        batch_size=2,
        n_steps=10,
    )
    agent_trainer = preference_comparisons.AgentTrainer(
        agent,
        reward_net,
        venv,
        exploration_frac=0.5,
    )
    trajectories = agent_trainer.sample(2)
    assert len(trajectories) > 0
    assert all(
        trajectory.obs.shape[1:] == venv.observation_space.shape
        for trajectory in trajectories
    )
