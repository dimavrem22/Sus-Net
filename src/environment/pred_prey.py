from src.metrics import SusMetrics
from src.environment.base import FourRoomEnv, IMPOSTER_ACTIONS, CREW_ACTIONS, Action

CREW_ACTIONS_SIMPLE = [
    Action.STAY,
    Action.UP,
    Action.DOWN,
    Action.LEFT,
    Action.RIGHT,
]

IMPOSTER_ACTIONS_SIMPLE = [
    Action.STAY,
    Action.UP,
    Action.DOWN,
    Action.LEFT,
    Action.RIGHT,
    Action.KILL,
]
class ImposterTrainingGround(FourRoomEnv):
    """
    A specialized environment where an imposter is trained to strategize against
    a set of crew members that perform random actions with equal probability.
    """

    def __init__(
        self,
        n_crew,
        n_jobs,
        time_step_reward,
        kill_reward,
        sabotage_reward,
        end_of_game_reward,
        random_state=None,
        debug=False,
        shuffle_imposter_index=False,
        include_walls: bool = True,
    ):
        """
        Initializes the ImposterTrainingGround environment.

        Parameters:
            n_crew (int): Number of crew members.
            n_jobs (int): Number of jobs available for crew members to complete.
            time_step_reward (float): Reward received at each timestep.
            kill_reward (float): Reward for killing a crew member.
            sabotage_reward (float): Reward for sabotaging a job.
            end_of_game_reward (float): Reward or penalty at the end of the game.
            random_state (int, optional): Seed for the random number generator.
            debug (bool): Flag to enable debugging outputs.
        """
        super().__init__(
            n_imposters=1,
            n_crew=n_crew,
            n_jobs=n_jobs,
            time_step_reward=time_step_reward,
            kill_reward=kill_reward,
            sabotage_reward=sabotage_reward,
            debug=debug,
            dead_penalty=0,
            game_end_reward=end_of_game_reward,
            random_state=random_state,
            is_action_order_random=False,
            shuffle_imposter_index=shuffle_imposter_index,
            include_walls=include_walls,
        )

        # override imposters' actions to not include sabotage
        self.imposter_actions = IMPOSTER_ACTIONS_SIMPLE
        self.n_imposter_actions = len(self.imposter_actions)

        self.crew_actions = CREW_ACTIONS_SIMPLE
        self.n_crew_actions = len(self.crew_actions)

    def _validate_init_args(self, n_imposters, n_crew, n_jobs):
        assert n_crew > 0, f"Must have at least one crew member. Got {n_crew}."

    def check_win_condition(self):
        """
                Checks if the game has reached a win condition for the imposter or the crew.
        s
                Returns:
                    tuple: A boolean indicating if the game has ended and the associated reward.
        """

        # all jobs are done imposter loses
        # NOTE: this is only possible if n_jobs is not 0
        if self.n_jobs != 0 and self.completed_jobs.sum() == self.n_jobs:
            self.logger.debug("CREW won!")
            self.metrics.update(SusMetrics.CREW_WON, 1)
            return True, self.game_end_reward

        # imposter wins bu killing all crew
        if self.alive_agents[self.crew_mask].sum() == 0:
            self.logger.debug("Imposters won!")
            self.metrics.update(SusMetrics.IMPOSTER_WON, 1)
            return True, -1 * self.game_end_reward

        return False, 0

