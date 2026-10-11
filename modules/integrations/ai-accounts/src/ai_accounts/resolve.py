"""Pick an account: flag, env, sticky, auto, default."""
import os


class UnknownAccount(Exception):
    def __init__(self, agent: str, name: str, valid: list):
        self.agent, self.name, self.valid = agent, name, valid
        super().__init__(f"{agent}: unknown account {name!r}; valid: {', '.join(valid)}")


def env_var(agent: str) -> str:
    return "AI_ACCOUNT_" + agent.upper().replace("-", "_")


def resolve(agent, accounts, *, flag=None, environ=None, sticky=None, select="manual", auto=None) -> str:
    if not accounts:
        return "default"
    environ = os.environ if environ is None else environ

    def known(name):
        if name not in accounts:
            raise UnknownAccount(agent, name, sorted(accounts))
        return name

    if flag:
        return known(flag)
    if environ.get(env_var(agent)):
        return known(environ[env_var(agent)])
    if sticky and sticky != "auto":
        return known(sticky)
    if (select == "auto" or sticky == "auto") and auto is not None:
        picked = auto()
        if picked in accounts:
            return picked
    return "default"
