from rlprenota.master.config import Config
from rlprenota.master.service import run

if __name__ == "__main__":
    run(Config.from_env())
