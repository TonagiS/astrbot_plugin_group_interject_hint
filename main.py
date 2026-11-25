"""给核心「主动回复」通路加一个提示头。

背景
----
AstrBot 核心的 provider_ltm_settings.active_reply（概率触发）会把一条群消息直接
送进 LLM，prompt 只有消息原文，没有任何「这不是发给你的」的标记。于是模型容易把
群友之间的闲聊当成在跟自己说话，回得像在回答问题，而不是随口插一句。

本插件只做一件事：在 on_llm_request 里判断「群消息 + 没有被 @ / 唤醒」，说明这次
回复是核心概率抽中的主动插话，就往 system prompt 末尾追加一段说明。

不碰的地方
----------
* 私聊：不注入
* 被 @ / 唤醒的消息（含 ds_alert 之类把事件标成 wake 的插件）：不注入
* 命中 direct wake word 的消息：同上，走 is_at_or_wake_command 判断
* 其它人格：由 only_for_personas 控制，默认为空（全部人格都注入）

和现有插件的关系
----------------
组群画像（group_profile）、身份标注（identity / dad_tag）改的也是提示词，但注入
位置与内容互不覆盖；本插件只追加、不覆盖，且用固定标记防重复注入。
"""

from __future__ import annotations

import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.platform import MessageType
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

PLUGIN_DIR = Path(__file__).resolve().parent
LOG_PATH = PLUGIN_DIR / "data" / "interject_hint.log"
MARKER = "【群聊插话】"

DEFAULT_HINT = (
    "【群聊插话】这条消息不是发给你的：群里别人在聊，没有人 @ 你，也没人叫你，是你自己决定插一句。\n"
    "- 当作旁听到的一句话，顺着他们的话题随口接一句就好；不要用回答提问的口吻，"
    "不要“你好”“有什么需要”这类开场；\n"
    "- 一到两句，越短越好；别复述这条消息、别总结、别列清单；\n"
    "- 这句要是实在不好接（私事、两人对话、你也看不懂），就用最轻的一句带过，别硬凑。"
)


def _log(msg: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 512 * 1024:
            LOG_PATH.write_text("", encoding="utf-8")
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:  # noqa: BLE001
        pass


class GroupInterjectHint(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._count = 0

    def _cfg(self, key: str, default=None):
        try:
            value = self.config.get(key, default)
        except Exception:  # noqa: BLE001
            value = default
        return default if value is None else value

    @staticmethod
    def _split(raw) -> list[str]:
        if isinstance(raw, str):
            raw = raw.replace("，", ",").split(",")
        if isinstance(raw, (list, tuple)):
            return [str(x).strip() for x in raw if str(x).strip()]
        return []

    def _group_allowed(self, group_id: str, umo: str) -> bool:
        groups = self._split(self._cfg("groups", []))
        if not groups:
            return True
        return (group_id and group_id in groups) or (umo and umo in groups)

    def _persona_allowed(self, prompt: str) -> bool:
        markers = self._split(self._cfg("only_for_personas", []))
        if not markers:
            return True
        return any(m in prompt for m in markers)

    @filter.on_llm_request()
    async def inject_hint(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not bool(self._cfg("enable", True)):
            return
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
        except Exception:  # noqa: BLE001
            return

        # 被 @ / 唤醒的消息是正常对话，不要加插话提示
        try:
            if bool(event.is_at_or_wake_command):
                return
        except Exception:  # noqa: BLE001
            pass

        try:
            group_id = str(event.get_group_id() or "")
            umo = str(event.unified_msg_origin or "")
        except Exception:  # noqa: BLE001
            group_id, umo = "", ""

        if not self._group_allowed(group_id, umo):
            return

        system_prompt = str(req.system_prompt or "")
        if MARKER in system_prompt:
            return
        if not self._persona_allowed(system_prompt):
            return

        hint = str(self._cfg("hint", "") or "").strip() or DEFAULT_HINT
        try:
            req.system_prompt = (system_prompt + "\n\n" + hint) if system_prompt else hint
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[interject_hint] 注入失败: {exc}")
            return

        self._count += 1
        text = ""
        try:
            text = str(event.message_str or "")[:30]
        except Exception:  # noqa: BLE001
            text = ""
        _log(f"注入群={group_id or umo} 第{self._count}次 消息={text!r}")
