import asyncio
from dataclasses import dataclass
import time
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from core.message import Message
from core.service import MessageHandler, MessagingService


@dataclass
class Session:
    user_id: str
    chat_id: str
    flow_name: str
    current_step: str
    answers: Dict[str, Any]
    last_message_id: Optional[str] = None
    step_stack: list[str] = None  # history for /back
    last_prompt_at: Optional[float] = None  # monotonic timestamp when prompt sent
    last_prompt_len: int = 0
    last_prompt_text: Optional[str] = None

@dataclass
class TypingProfile:
    cps: float = 18.0  # characters per second
    samples: int = 0
    errors: int = 0


class QuestionnaireHandler(MessageHandler):
    """
    YAML-driven questionnaire/state-machine plugin.

    Flow YAML structure example:
    ---
    name: onboarding
    start: ask_name
    steps:
      ask_name:
        prompt: "Hi! What's your name?"
        var: name
        validate: "len(text) >= 2"
        error: "Please enter at least 2 characters."
        next: ask_nickname
      ask_nickname:
        prompt: "What should I call you?"
        var: nickname
        next: summary
      summary:
        prompt: "Thanks {name}! I'll call you {nickname}."
        end: true
    triggers:
      commands: ["/start", "/onboard", "/questionnaire"]
    """

    def __init__(self, flow_path: str, alias: Optional[str] = None):
        self.flow_path = Path(flow_path)
        with open(self.flow_path, "r") as f:
            self.flow = yaml.safe_load(f)
        self.sessions: Dict[str, Session] = {}
        self.triggers = set(self.flow.get("triggers", {}).get("commands", ["/questionnaire"]))
        self.alias = alias or self.flow.get("name", "flow")
        # Optional global reveal config, e.g.:
        # reveal: { type: "typewriter", enabled: true, cps: 18, chunk: 2, jitter_ms: 40 }
        self.global_reveal = self.flow.get("reveal", {}) or {}
        # Per-user typing profiles (in-memory)
        self.user_profiles: Dict[str, TypingProfile] = {}

    def _key(self, chat_id: str, user_id: str) -> str:
        return f"{chat_id}:{user_id}"

    def _is_trigger(self, text: str) -> bool:
        return text.strip().split(" ")[0] in self.triggers

    async def handle(self, message: Message, service: MessagingService) -> bool:
        text = (message.content or "").strip()
        key = self._key(message.chat.id, message.from_user.id)

        # Start session on trigger
        if self._is_trigger(text):
            await self.start_session(
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                service=service,
            )
            return True

        # Continue existing session
        if key in self.sessions:
            session = self.sessions[key]
            step = self.flow["steps"].get(session.current_step, {})

            # Update typing profile from user response timing
            if session.last_prompt_at is not None:
                dt = max(0.1, time.monotonic() - session.last_prompt_at)
                observed_cps = max(1.0, min(60.0, len(text) / dt))
                prof = self.user_profiles.get(session.user_id) or TypingProfile()
                alpha = 0.3
                prof.cps = max(3.0, min(45.0, alpha * observed_cps + (1 - alpha) * prof.cps))
                prof.samples += 1
                self.user_profiles[session.user_id] = prof

            # Controls: /back and /cancel
            if text.lower().startswith("/cancel"):
                await service.send_message(chat_id=session.chat_id, text="Session canceled.")
                del self.sessions[key]
                return True
            if text.lower().startswith("/back"):
                if session.step_stack and len(session.step_stack) > 0:
                    session.current_step = session.step_stack.pop()
                    await self._send_prompt(message, service, key)
                else:
                    await service.send_message(chat_id=session.chat_id, text="No previous step.")
                return True

            # Collect input if step expects it (supports options or free text)
            var = step.get("var")
            if var:
                selected_value: Optional[Any] = None

                options = step.get("options") or []
                if options:
                    # Accept numeric selection or text/value match
                    idx = None
                    if text.isdigit():
                        i = int(text)
                        if 1 <= i <= len(options):
                            idx = i - 1
                    if idx is None:
                        # try text/value match (case-insensitive)
                        for i, opt in enumerate(options):
                            t = str(opt.get("text", "")).strip().lower()
                            v = str(opt.get("value", t)).strip().lower()
                            if text.strip().lower() in (t, v):
                                idx = i
                                break
                    if idx is None:
                        await service.send_message(chat_id=session.chat_id, text="Please choose a valid option.")
                        return True
                    choice = options[idx]
                    selected_value = choice.get("value", choice.get("text"))
                    # Store a flag for per-option next override
                    step["_chosen_next"] = choice.get("next")
                else:
                    # Free text with validation
                    valid, error_msg = self._validate_input(step, text, session)
                    if not valid:
                        # bump error count (can be used for future tuning)
                        prof = self.user_profiles.get(session.user_id) or TypingProfile()
                        prof.errors += 1
                        self.user_profiles[session.user_id] = prof
                        await service.send_message(chat_id=session.chat_id, text=error_msg or "Invalid input. Try again.")
                        return True
                    selected_value = text

                session.answers[var] = selected_value

            # Next step resolution: per-option next, conditional next_if, else default 'next' or end
            # 1) per-option override
            next_step = step.get("_chosen_next") or step.get("next")
            if "_chosen_next" in step:
                del step["_chosen_next"]

            # 2) conditional routing
            if step.get("next_if"):
                rules = step["next_if"]
                next_from_rules: Optional[str] = None
                for rule in rules:
                    if "when" in rule:
                        try:
                            ok = bool(eval(str(rule["when"]), {"__builtins__": {}}, {**session.answers}))
                        except Exception:
                            ok = False
                        if ok:
                            next_from_rules = rule.get("next")
                            break
                    elif "else" in rule:
                        next_from_rules = rule.get("else")
                if next_from_rules:
                    next_step = next_from_rules
            end = step.get("end", False)
            if end and not next_step:
                await self._send_summary(session, service)
                del self.sessions[key]
                return True

            if next_step:
                # push current step to stack for /back
                if session.step_stack is None:
                    session.step_stack = []
                session.step_stack.append(session.current_step)
                session.current_step = next_step
                await self._send_prompt(message, service, key)
                return True

            # If no next or end, stay
            await service.send_message(chat_id=session.chat_id, text="Waiting for next instruction...")
            return True

        return False

    async def start_session(
        self,
        chat_id: str,
        user_id: str,
        service: MessagingService,
        step: Optional[str] = None,
        preset_answers: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Programmatically start a session (used by StartRouter)."""
        key = self._key(chat_id, user_id)
        start_step = step or self.flow.get("start")
        if not start_step:
            return
        self.sessions[key] = Session(
            user_id=user_id,
            chat_id=chat_id,
            flow_name=self.flow.get("name", "flow"),
            current_step=start_step,
            answers=preset_answers or {},
        )
        # Send first prompt
        dummy_message = Message(
            message_id="0",
            from_user=type("U", (), {"id": user_id})(),  # minimal shim, not used in prompt
            chat=type("C", (), {"id": chat_id})(),
            date=None,  # not used
            message_type=None,
            content="",
            raw_data={},
        )
        await self._send_prompt(dummy_message, service, key)

    async def _send_prompt(self, message: Message, service: MessagingService, key: str):
        session = self.sessions[key]
        step = self.flow["steps"].get(session.current_step, {})

        # Render prompt using answers
        prompt_template = step.get("prompt", "")
        prompt = prompt_template.format(**session.answers)

        # If the prompt to send is identical to the last one we rendered, skip to avoid redundant rewrites
        if session.last_prompt_text == prompt:
            return

        # Build optional keyboard from options with config
        options = step.get("options") or []
        # keyboard config: step overrides global
        kb_cfg = {}
        if isinstance(self.flow.get("keyboard"), dict):
            kb_cfg.update(self.flow.get("keyboard"))
        if isinstance(step.get("keyboard"), dict):
            kb_cfg.update(step.get("keyboard"))
        kb_enabled = bool(kb_cfg.get("enabled", True))
        kb_columns = int(kb_cfg.get("columns", 2))
        remove_when_no_options = bool(kb_cfg.get("remove_when_no_options", True))

        keyboard_rows = None
        if kb_enabled and isinstance(options, list) and options:
            # Make rows of up to kb_columns buttons
            keyboard_rows = []
            row = []
            for opt in options:
                label = str(opt.get("text", opt.get("value", "")))
                if not label:
                    continue
                row.append(label)
                if len(row) == max(1, kb_columns):
                    keyboard_rows.append(row)
                    row = []
            if row:
                keyboard_rows.append(row)
        # Decide if we should explicitly remove keyboard when none is shown
        should_remove_keyboard = keyboard_rows is None and remove_when_no_options

        # Determine reveal config (step overrides global)
        step_reveal = step.get("reveal", None)
        reveal_cfg = step_reveal if step_reveal is not None else self.global_reveal
        reveal_type = None
        reveal_enabled = False
        # base cps defaults, may be replaced by user profile
        cps = 18
        chunk = 2
        jitter_ms = 40
        typos_prob = 0.0
        correction_delay_ms = 120
        max_typos = 2
        punctuation_ms = 120
        min_edit_interval_ms = 90
        head_chars = None  # if set, typewriter only first N then jump to full text
        mode = "inline"
        inline_threshold = 80
        if isinstance(reveal_cfg, bool):
            reveal_enabled = reveal_cfg
            reveal_type = "typewriter" if reveal_enabled else None
        elif isinstance(reveal_cfg, str):
            reveal_enabled = True
            reveal_type = reveal_cfg
        elif isinstance(reveal_cfg, dict):
            reveal_type = reveal_cfg.get("type", "typewriter")
            reveal_enabled = reveal_cfg.get("enabled", True)
            cps = int(reveal_cfg.get("cps", cps))
            chunk = max(1, int(reveal_cfg.get("chunk", chunk)))
            jitter_ms = int(reveal_cfg.get("jitter_ms", jitter_ms))
            typos_prob = float(reveal_cfg.get("typos_prob", typos_prob))
            correction_delay_ms = int(reveal_cfg.get("correction_delay_ms", correction_delay_ms))
            max_typos = int(reveal_cfg.get("max_typos", max_typos))
            punctuation_ms = int(reveal_cfg.get("punctuation_ms", punctuation_ms))
            min_edit_interval_ms = int(reveal_cfg.get("min_edit_interval_ms", min_edit_interval_ms))
            head_chars = reveal_cfg.get("head_chars", head_chars)
            mode = str(reveal_cfg.get("mode", mode)).strip().lower()
            inline_threshold = int(reveal_cfg.get("inline_threshold", inline_threshold))
            if isinstance(head_chars, str):
                try:
                    head_chars = int(head_chars)
                except Exception:
                    head_chars = None

        # If we have a user typing profile, let it override cps subtly
        prof = self.user_profiles.get(session.user_id)
        if prof:
            cps = int(max(3, min(45, prof.cps)))

        # Decide whether to do inline typewriter or direct send based on mode/smart
        do_inline = reveal_enabled and reveal_type == "typewriter"
        if mode == "new":
            do_inline = False
        elif mode == "smart" and len(prompt) > inline_threshold:
            do_inline = False

        # If reveal is enabled and inline is allowed, prefer it over loader
        if do_inline:
            # Ensure we have a message to edit
            if not session.last_message_id:
                if keyboard_rows:
                    sent = await service.send_message(chat_id=session.chat_id, text="…", keyboard=keyboard_rows)
                else:
                    if should_remove_keyboard:
                        sent = await service.send_message(chat_id=session.chat_id, text="…", remove_keyboard=True)
                    else:
                        sent = await service.send_message(chat_id=session.chat_id, text="…")
                session.last_message_id = str(getattr(sent, "message_id", "") or "")
            else:
                try:
                    if keyboard_rows:
                        await service.edit_message_text(
                            chat_id=session.chat_id,
                            message_id=session.last_message_id,
                            text="…",
                            keyboard=keyboard_rows,
                        )
                    else:
                        await service.edit_message_text(
                            chat_id=session.chat_id,
                            message_id=session.last_message_id,
                            text="…",
                            remove_keyboard=True,
                        )
                except Exception:
                    if keyboard_rows:
                        sent = await service.send_message(chat_id=session.chat_id, text="…", keyboard=keyboard_rows)
                    else:
                        sent = await service.send_message(chat_id=session.chat_id, text="…", remove_keyboard=True)
                    session.last_message_id = str(getattr(sent, "message_id", "") or "")

            # Simulate typing by incremental edits
            import random
            # time per char
            delay_per_char = max(0.01, 1.0 / max(3, cps))
            # compute effective chunk to keep edit rate reasonable
            min_edit_interval_s = max(0.03, min_edit_interval_ms / 1000.0)
            eff_chunk = max(1, int(max(chunk, cps * min_edit_interval_s)))

            text_so_far = ""
            typos_used = 0
            last_edit_ts = 0.0
            edits = 0
            for i in range(0, len(prompt), eff_chunk):
                text_so_far = prompt[: i + eff_chunk]
                # throttle edits to avoid spamming Telegram API
                now = asyncio.get_event_loop().time()
                sleep_needed = min_edit_interval_s - (now - last_edit_ts)
                if sleep_needed > 0:
                    await asyncio.sleep(sleep_needed)
                try:
                    if keyboard_rows:
                        await service.edit_message_text(
                            chat_id=session.chat_id,
                            message_id=session.last_message_id,
                            text=text_so_far,
                            keyboard=keyboard_rows,
                        )
                    else:
                        await service.edit_message_text(
                            chat_id=session.chat_id,
                            message_id=session.last_message_id,
                            text=text_so_far,
                            remove_keyboard=True,
                        )
                    last_edit_ts = asyncio.get_event_loop().time()
                except Exception:
                    # If edit fails, fall back to sending once and stop reveal
                    if keyboard_rows:
                        sent = await service.send_message(chat_id=session.chat_id, text=prompt, keyboard=keyboard_rows)
                    else:
                        if should_remove_keyboard:
                            sent = await service.send_message(chat_id=session.chat_id, text=prompt, remove_keyboard=True)
                        else:
                            sent = await service.send_message(chat_id=session.chat_id, text=prompt)
                    session.last_message_id = str(getattr(sent, "message_id", "") or "")
                    break
                # keep chat action alive periodically
                edits += 1
                if edits % 2 == 0:
                    await service.send_chat_action(session.chat_id, "typing")
                # add a tiny jitter
                jitter = random.uniform(0.0, max(0.0, jitter_ms) / 1000.0)
                await asyncio.sleep(delay_per_char * eff_chunk + jitter)
                # pause slightly after punctuation for realism
                if text_so_far and text_so_far[-1] in ",.!?;:":
                    await asyncio.sleep(max(0.0, punctuation_ms) / 1000.0)

                # Optional: simulate a small typo then correct it
                if typos_used < max_typos and typos_prob > 0 and random.random() < typos_prob and len(text_so_far) > 2:
                    wrong_char = random.choice("abcdefghijklmnopqrstuvwxyz")
                    try:
                        if keyboard_rows:
                            await service.edit_message_text(
                                chat_id=session.chat_id,
                                message_id=session.last_message_id,
                                text=text_so_far + wrong_char,
                                keyboard=keyboard_rows,
                            )
                        else:
                            await service.edit_message_text(
                                chat_id=session.chat_id,
                                message_id=session.last_message_id,
                                text=text_so_far + wrong_char,
                                remove_keyboard=True,
                            )
                        await asyncio.sleep(max(0.05, correction_delay_ms / 1000.0))
                        if keyboard_rows:
                            await service.edit_message_text(
                                chat_id=session.chat_id,
                                message_id=session.last_message_id,
                                text=text_so_far,
                                keyboard=keyboard_rows,
                            )
                        else:
                            await service.edit_message_text(
                                chat_id=session.chat_id,
                                message_id=session.last_message_id,
                                text=text_so_far,
                                remove_keyboard=True,
                            )
                        typos_used += 1
                    except Exception:
                        pass
                # If head_chars configured and we've shown enough, jump to full text
                if head_chars is not None and len(text_so_far) >= int(head_chars):
                    try:
                        if keyboard_rows:
                            await service.edit_message_text(
                                chat_id=session.chat_id,
                                message_id=session.last_message_id,
                                text=prompt,
                                keyboard=keyboard_rows,
                            )
                        else:
                            await service.edit_message_text(
                                chat_id=session.chat_id,
                                message_id=session.last_message_id,
                                text=prompt,
                                remove_keyboard=True,
                            )
                    except Exception:
                        pass
                    break
            else:
                # final ensure full text
                try:
                    if keyboard_rows:
                        await service.edit_message_text(
                            chat_id=session.chat_id,
                            message_id=session.last_message_id,
                            text=prompt,
                            keyboard=keyboard_rows,
                        )
                    else:
                        await service.edit_message_text(
                            chat_id=session.chat_id,
                            message_id=session.last_message_id,
                            text=prompt,
                            remove_keyboard=True,
                        )
                except Exception:
                    pass

            # Record prompt timing for next response measurement
            session.last_prompt_at = time.monotonic()
            session.last_prompt_len = len(prompt)
            session.last_prompt_text = prompt

        # Optional loader animation via message edit (only if reveal not used)
        elif step.get("loader", False):
            # In 'new' mode, skip loader/editing and just send the full prompt as a new message
            if mode == "new":
                if keyboard_rows:
                    sent = await service.send_message(chat_id=session.chat_id, text=prompt, keyboard=keyboard_rows)
                else:
                    if should_remove_keyboard:
                        sent = await service.send_message(chat_id=session.chat_id, text=prompt, remove_keyboard=True)
                    else:
                        sent = await service.send_message(chat_id=session.chat_id, text=prompt)
                # Do not adopt last_message_id in 'new' mode to avoid future edits
                session.last_prompt_at = time.monotonic()
                session.last_prompt_len = len(prompt)
                session.last_prompt_text = prompt
                return
            # Reuse existing message if we have one, otherwise send a fresh loader message
            if not session.last_message_id:
                if keyboard_rows:
                    msg = await service.send_message(chat_id=session.chat_id, text="Loader .", keyboard=keyboard_rows)
                else:
                    if should_remove_keyboard:
                        msg = await service.send_message(chat_id=session.chat_id, text="Loader .", remove_keyboard=True)
                    else:
                        msg = await service.send_message(chat_id=session.chat_id, text="Loader .")
                session.last_message_id = str(getattr(msg, "message_id", "") or "")
            else:
                # Initialize loader text in the existing message
                try:
                    if keyboard_rows:
                        await service.edit_message_text(
                            chat_id=session.chat_id,
                            message_id=session.last_message_id,
                            text="Loader .",
                            keyboard=keyboard_rows,
                        )
                    else:
                        await service.edit_message_text(
                            chat_id=session.chat_id,
                            message_id=session.last_message_id,
                            text="Loader .",
                            remove_keyboard=True,
                        )
                except Exception:
                    # If we cannot edit the old one, send new once and stick to that ID
                    if keyboard_rows:
                        msg = await service.send_message(chat_id=session.chat_id, text="Loader .", keyboard=keyboard_rows)
                    else:
                        if should_remove_keyboard:
                            msg = await service.send_message(chat_id=session.chat_id, text="Loader .", remove_keyboard=True)
                        else:
                            msg = await service.send_message(chat_id=session.chat_id, text="Loader .")
                    session.last_message_id = str(getattr(msg, "message_id", "") or "")

            # Animate in-place; if an edit fails, skip further loader updates to avoid spam
            loader_failed = False
            for dot_count in [2, 3, 1]:
                await asyncio.sleep(0.4)
                if loader_failed:
                    continue
                try:
                    if keyboard_rows:
                        await service.edit_message_text(
                            chat_id=session.chat_id,
                            message_id=session.last_message_id,
                            text=f"Loader {'.' * dot_count}",
                            keyboard=keyboard_rows,
                        )
                    else:
                        await service.edit_message_text(
                            chat_id=session.chat_id,
                            message_id=session.last_message_id,
                            text=f"Loader {'.' * dot_count}",
                            remove_keyboard=True,
                        )
                except Exception:
                    loader_failed = True

            await asyncio.sleep(0.3)
            try:
                if keyboard_rows:
                    await service.edit_message_text(
                        chat_id=session.chat_id,
                        message_id=session.last_message_id,
                        text=prompt,
                        keyboard=keyboard_rows,
                    )
                else:
                    await service.edit_message_text(
                        chat_id=session.chat_id,
                        message_id=session.last_message_id,
                        text=prompt,
                        remove_keyboard=True if should_remove_keyboard else None,
                    )
            except Exception:
                # Final fallback: send new prompt message and adopt its ID
                if keyboard_rows:
                    sent = await service.send_message(chat_id=session.chat_id, text=prompt, keyboard=keyboard_rows)
                else:
                    if should_remove_keyboard:
                        sent = await service.send_message(chat_id=session.chat_id, text=prompt, remove_keyboard=True)
                    else:
                        sent = await service.send_message(chat_id=session.chat_id, text=prompt)
                session.last_message_id = str(getattr(sent, "message_id", "") or "") or session.last_message_id
            session.last_prompt_at = time.monotonic()
            session.last_prompt_len = len(prompt)
            session.last_prompt_text = prompt
        else:
            # Simulate typing duration proportional to text length (capped)
            cycles = max(1, min(5, len(prompt) // 25))
            for _ in range(cycles):
                await service.send_chat_action(session.chat_id, "typing")
                await asyncio.sleep(0.6)

            if mode == "new":
                # Always send a fresh message and do not adopt the ID
                if keyboard_rows:
                    sent = await service.send_message(chat_id=session.chat_id, text=prompt, keyboard=keyboard_rows)
                else:
                    if should_remove_keyboard:
                        sent = await service.send_message(chat_id=session.chat_id, text=prompt, remove_keyboard=True)
                    else:
                        sent = await service.send_message(chat_id=session.chat_id, text=prompt)
                session.last_prompt_at = time.monotonic()
                session.last_prompt_len = len(prompt)
                session.last_prompt_text = prompt
            else:
                # Prefer editing the existing bot message to keep the thread tidy
                if session.last_message_id:
                    try:
                        if keyboard_rows:
                            await service.edit_message_text(
                                chat_id=session.chat_id,
                                message_id=session.last_message_id,
                                text=prompt,
                                keyboard=keyboard_rows,
                            )
                        else:
                            await service.edit_message_text(
                                chat_id=session.chat_id,
                                message_id=session.last_message_id,
                                text=prompt,
                                remove_keyboard=True if should_remove_keyboard else None,
                            )
                    except Exception:
                        # Final fallback: send new prompt message and adopt its ID
                        if keyboard_rows:
                            sent = await service.send_message(chat_id=session.chat_id, text=prompt, keyboard=keyboard_rows)
                        else:
                            if should_remove_keyboard:
                                sent = await service.send_message(chat_id=session.chat_id, text=prompt, remove_keyboard=True)
                            else:
                                sent = await service.send_message(chat_id=session.chat_id, text=prompt)
                        session.last_message_id = str(getattr(sent, "message_id", "") or "") or session.last_message_id
                else:
                    if keyboard_rows:
                        sent = await service.send_message(chat_id=session.chat_id, text=prompt, keyboard=keyboard_rows)
                    else:
                        if should_remove_keyboard:
                            sent = await service.send_message(chat_id=session.chat_id, text=prompt, remove_keyboard=True)
                        else:
                            sent = await service.send_message(chat_id=session.chat_id, text=prompt)
                    session.last_message_id = str(getattr(sent, "message_id", "") or "")
                session.last_prompt_at = time.monotonic()
                session.last_prompt_len = len(prompt)
                session.last_prompt_text = prompt

    async def _send_summary(self, session: Session, service: MessagingService):
        # Default summary uses the final step's prompt rendered
        step = self.flow["steps"].get(session.current_step, {})
        prompt_template = step.get("prompt", "")
        prompt = prompt_template.format(**session.answers)
        await service.send_chat_action(session.chat_id, "typing")
        await asyncio.sleep(0.2)
        await service.send_message(chat_id=session.chat_id, text=prompt, remove_keyboard=True)

    def _validate_input(self, step: Dict[str, Any], text: str, session: Session) -> (bool, Optional[str]):
        rule = step.get("validate")
        if not rule:
            return True, None
        # Allow a tiny whitelist of safe functions so rules like "len(text) >= 2" work
        safe_globals = {"__builtins__": {}, "len": len, "min": min, "max": max, "int": int, "float": float, "str": str}
        safe_locals = {"text": text, **session.answers}
        try:
            ok = bool(eval(str(rule), safe_globals, safe_locals))
        except Exception:
            ok = False
        if not ok:
            return False, step.get("error")
        return True, None
