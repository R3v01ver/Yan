"""砚（Yan）— AI 圆桌：各家拿出办法，互相取长补短，综合成一套方案。"""

from . import providers
from .council import Council, Result
from .delegate import Delegate, ROLE_BRIEFS
from .transcript import Transcript

__version__ = "0.1.0"

__all__ = ["Council", "Result", "Delegate", "ROLE_BRIEFS", "Transcript", "providers"]
