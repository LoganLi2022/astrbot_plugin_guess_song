import asyncio
import random
import subprocess
import tempfile
import time
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
from astrbot.core.utils.session_waiter import (
    USER_SESSIONS,
    SessionController,
    SessionFilter,
    session_waiter,
)

PLUGIN_NAME = "astrbot_plugin_guess_song"
EXIT_COMMANDS = frozenset(
    {
        "退出",
        "结束",
        "quit",
        "exit",
        "q",
        "猜歌迷退出",
        "结束游戏",
    },
)


def _game_session_id(event: AstrMessageEvent) -> str:
    """Return the session key shared by game state and session_waiter."""
    return event.get_group_id() or event.unified_msg_origin


class CustomFilter(SessionFilter):
    def filter(self, event: AstrMessageEvent) -> str:
        return _game_session_id(event)


@register(
    PLUGIN_NAME,
    "Logan",
    "一个猜歌迷插件，支持音频剪辑、计分和多轮游戏",
    "1.0.0",
)
class GuessSongPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.plugin_data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME

        # 游戏状态存储
        self.game_sessions = {}  # {session_id: game_data}

        # 消息模板
        self.messages = {
            "start": [
                "🎵 猜歌迷游戏开始啦！我会播放一段音乐片段，猜猜是哪首歌吧！",
                "🎶 听歌识曲时间到！准备好了吗？",
                "✨ 音乐挑战开始！竖起你的小耳朵~",
            ],
            "hint": [
                "💡 提示一下：这首歌的演唱者是 {}",
                "🔍 给个小提示：这首歌发行于 {} 年",
                "🎤 歌词里有 '{}' 哦~",
                "📝 这首歌的风格是 {}",
            ],
            "wrong": [
                "❌ 不对哦，再想想看！",
                "😅 差一点点，继续加油！",
                "🤔 这个答案不太对呢~",
                "💪 再猜猜看！",
            ],
            "correct": [
                "🎉 太棒啦！答对了！+1分",
                "🌟 完全正确！你真是个小曲库！",
                "✨ 答对咯！继续加油！",
                "🎊 厉害厉害！又得一分！",
            ],
            "timeout": [
                "⏰ 时间到啦！正确答案是：《{}》",
                "⌛ 超时咯！这首歌是《{}》",
            ],
            "end": [
                "🏆 游戏结束！",
                "🎮 结算时间！",
                "📊 {}轮游戏结束！",
            ],
            "exit": [
                "👋 游戏已退出！",
                "🚪 主动退出游戏！",
            ],
            "auto_end_no_answer": "😴 连续 {} 轮无人作答，游戏已自动结束。",
            "no_songs": "😅 没有可用的歌曲，请先在插件配置中上传音乐文件！",
            "round_info": "🎯 第 {}/{} 轮",
        }

        self.song_data = self._load_songs_from_config()

    def _meta_by_filename(self) -> dict[str, dict]:
        """Build a lookup table for optional per-song metadata."""
        meta_map: dict[str, dict] = {}
        for item in self.config.get("song_meta", []):
            if not isinstance(item, dict):
                continue
            file_name = str(item.get("file_name", "")).strip()
            if not file_name:
                continue
            meta_map[file_name] = item
            meta_map[Path(file_name).name] = item
        return meta_map

    def _resolve_song_path(self, file_ref: str) -> Path | None:
        """Resolve a configured song path to an absolute file path."""
        if not file_ref:
            return None

        normalized = file_ref.replace("\\", "/").lstrip("/")
        plugin_root = self.plugin_data_dir.resolve(strict=False)

        if normalized.startswith("files/"):
            candidate = (plugin_root / normalized).resolve(strict=False)
            try:
                candidate.relative_to(plugin_root)
            except ValueError:
                return None
            if candidate.is_file():
                return candidate

        direct = Path(file_ref)
        if direct.is_file():
            return direct.resolve(strict=False)

        fallback = (plugin_root / normalized).resolve(strict=False)
        try:
            fallback.relative_to(plugin_root)
        except ValueError:
            return None
        if fallback.is_file():
            return fallback

        return None

    def _default_answers(self, file_name: str) -> list[str]:
        stem = Path(file_name).stem
        return [stem] if stem else []

    def _song_entry_from_path(
        self,
        file_ref: str,
        extra: dict | None = None,
    ) -> dict | None:
        """Build one song record from a configured path and optional metadata."""
        extra = extra or {}
        file_name = str(extra.get("file_name", "")).strip() or Path(file_ref).name
        resolved = self._resolve_song_path(file_ref)
        if resolved is None:
            logger.warning(f"歌曲文件不存在: {file_ref}")
            return None

        answers = extra.get("answers") or []
        if not isinstance(answers, list):
            answers = []
        if not answers:
            answers = self._default_answers(file_name)

        return {
            "file_path": str(resolved),
            "answers": answers,
            "artist": extra.get("artist", ""),
            "year": extra.get("year", ""),
            "lyric": extra.get("lyric", ""),
            "genre": extra.get("genre", ""),
        }

    def _load_songs_from_config(self) -> list[dict]:
        """Load songs from Dashboard file config and optional metadata."""
        songs: list[dict] = []
        meta_map = self._meta_by_filename()

        for entry in self.config.get("song_files", []):
            if isinstance(entry, str):
                file_name = Path(entry).name
                song = self._song_entry_from_path(entry, meta_map.get(file_name, {}))
            elif isinstance(entry, dict):
                file_ref = entry.get("file_path") or entry.get("file_name", "")
                file_name = str(entry.get("file_name", "")).strip() or Path(
                    str(file_ref),
                ).name
                merged = {**meta_map.get(file_name, {}), **entry}
                song = self._song_entry_from_path(str(file_ref), merged)
            else:
                logger.warning(f"忽略无法识别的歌曲配置项: {entry!r}")
                continue

            if song:
                songs.append(song)

        return songs

    async def initialize(self):
        """初始化插件"""
        self.song_data = self._load_songs_from_config()
        logger.info(f"🎵 猜歌迷插件已初始化！加载了 {len(self.song_data)} 首歌曲")
        try:
            subprocess.run(
                ["ffmpeg", "-version"],
                capture_output=True,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            logger.warning(
                "⚠️ ffmpeg 未安装或不在 PATH 中，音频裁剪功能可能无法使用",
            )

    def _extract_audio_clip(self, file_path: str, duration: int = 5) -> tuple[str | None, float]:
        """随机截取音频片段并转换为 wav 格式。"""
        try:
            cmd_duration = [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                file_path,
            ]
            result = subprocess.run(
                cmd_duration,
                capture_output=True,
                text=True,
                check=True,
            )
            total_duration = float(result.stdout.strip())

            if total_duration <= duration:
                start_time = 0
                actual_duration = total_duration
            else:
                max_start = total_duration - duration
                start_time = random.uniform(0, max_start)
                actual_duration = duration

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
                temp_path = tmp_file.name

            cmd_extract = [
                "ffmpeg",
                "-ss",
                str(start_time),
                "-i",
                file_path,
                "-t",
                str(actual_duration),
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                temp_path,
                "-y",
            ]
            subprocess.run(cmd_extract, capture_output=True, check=True)
            return temp_path, actual_duration

        except Exception as e:
            logger.error(f"音频截取失败: {e}")
            return None, 0

    def _get_answer_choices(self, correct_answers, count=3):
        """获取干扰项（非正确答案）"""
        all_answers = set()
        for song in self.song_data:
            all_answers.update(song.get("answers", []))

        correct_set = (
            set(correct_answers)
            if isinstance(correct_answers, list)
            else {correct_answers}
        )
        wrong_answers = list(all_answers - correct_set)

        if len(wrong_answers) < count:
            default_wrong = ["未知歌曲", "猜不出来", "太简单了", "这是啥歌"]
            wrong_answers.extend(default_wrong)
            wrong_answers = wrong_answers[:count]
        else:
            wrong_answers = random.sample(wrong_answers, count)

        return wrong_answers

    async def _send_audio_file(
        self,
        event: AstrMessageEvent,
        file_path: str,
        *,
        delete_after_send: bool = False,
    ) -> bool:
        """发送音频文件到会话。"""
        try:
            audio_msg = Comp.Record.fromFileSystem(file_path)
            result = event.make_result()
            result.chain = [audio_msg]
            await event.send(result)

            if delete_after_send:
                try:
                    Path(file_path).unlink(missing_ok=True)
                except OSError:
                    pass

            return True

        except Exception as e:
            logger.error(f"发送音频失败: {e}")
            await event.send(
                event.plain_result("🎵 (音频文件发送失败，请检查配置或联系管理员)"),
            )
            return False

    def _remember_player_name(
        self,
        game: dict,
        user_id: str,
        event: AstrMessageEvent,
    ) -> None:
        """Cache the player's nickname for leaderboard display."""
        if not user_id:
            return
        nickname = event.get_sender_name().strip()
        if nickname:
            game.setdefault("nicknames", {})[user_id] = nickname

    def _player_display_name(self, game: dict, user_id: str) -> str:
        """Return a display name for leaderboard rows."""
        nickname = game.get("nicknames", {}).get(user_id, "").strip()
        if nickname:
            return nickname
        if user_id:
            return f"用户{user_id[-4:]}"
        return "未知用户"

    async def _send_leaderboard(
        self,
        event: AstrMessageEvent,
        game: dict,
        is_exit: bool = False,
    ):
        """生成并发送排行榜"""
        if not game["scores"]:
            await event.send(event.plain_result("📊 本轮游戏没有人得分哦~"))
            return

        sorted_scores = sorted(
            game["scores"].items(),
            key=lambda x: x[1],
            reverse=True,
        )

        title = "👋 游戏退出结算" if is_exit else "🏆 游戏结束结算"
        leaderboard = f"{title}\n📊 排行榜：\n"

        medals = ["🥇", "🥈", "🥉"]
        for i, (user_id, score) in enumerate(sorted_scores[:10], 1):
            user_name = self._player_display_name(game, user_id)
            medal = medals[i - 1] if i <= 3 else f"{i}."
            leaderboard += f"{medal} {user_name}: {score}分\n"

        leaderboard += f"\n👥 参与人数：{len(game['scores'])} 人"

        if sorted_scores:
            top_user_id = sorted_scores[0][0]
            top_user = self._player_display_name(game, top_user_id)
            top_score = sorted_scores[0][1]
            leaderboard += f"\n👑 最高分：{top_user}（{top_score}分）"

        await event.send(event.plain_result(leaderboard))

    def _normalize_user_input(self, text: str) -> str:
        """Normalize user text for command and answer matching."""
        return text.strip()

    def _is_exit_command(self, text: str) -> bool:
        """Return whether the user message is an exit command."""
        raw = self._normalize_user_input(text)
        if not raw:
            return False
        lowered = raw.lower()
        if raw in EXIT_COMMANDS or lowered in EXIT_COMMANDS:
            return True
        for prefix in ("/", "!"):
            if lowered.startswith(prefix):
                body = lowered[1:].strip()
                if body in EXIT_COMMANDS:
                    return True
        return False

    def _touch_session_event(self, session_id: str, event: AstrMessageEvent) -> None:
        """Keep a recent event for the session so replies target the right chat."""
        game = self.game_sessions.get(session_id)
        if game:
            game["anchor_event"] = event
            self._remember_player_name(game, event.get_sender_id(), event)

    def _stop_session_waiter(self, session_id: str) -> None:
        """Stop an active session_waiter for the given game session."""
        session = USER_SESSIONS.get(session_id)
        if session and not session.session_controller.future.done():
            session.session_controller.stop()

    def _cancel_half_timeout_hint_task(self, game: dict) -> None:
        """Cancel the scheduled half-timeout hint task for the current round."""
        task = game.get("hint_task")
        if task and not task.done():
            task.cancel()
        game["hint_task"] = None

    def _should_send_half_timeout_hint(
        self,
        controller: SessionController,
        game: dict,
    ) -> bool:
        """Return whether half of the round timeout has elapsed."""
        if game.get("hint_sent") or game.get("used_hint"):
            return False
        if controller.ts is None or controller.timeout is None:
            return False
        elapsed = time.time() - controller.ts
        return elapsed >= controller.timeout / 2

    async def _schedule_half_timeout_hint(self, session_id: str) -> None:
        """Send a hint automatically when half of the round timeout elapses."""
        game = self.game_sessions.get(session_id)
        if not game:
            return
        try:
            await asyncio.sleep(game["timeout"] / 2)
        except asyncio.CancelledError:
            return

        game = self.game_sessions.get(session_id)
        if not game or not game["is_active"]:
            return
        if game.get("hint_sent") or game.get("used_hint"):
            return

        anchor_event = game.get("anchor_event")
        if anchor_event is None:
            return
        await self._send_hint(anchor_event, session_id)

    def _start_half_timeout_hint_task(self, session_id: str) -> None:
        """Schedule half-timeout hint for the current round."""
        game = self.game_sessions.get(session_id)
        if not game:
            return
        self._cancel_half_timeout_hint_task(game)
        game["hint_task"] = asyncio.create_task(
            self._schedule_half_timeout_hint(session_id),
        )

    async def _wait_for_guess_input(
        self,
        anchor_event: AstrMessageEvent,
        session_id: str,
    ) -> None:
        """Wait for one round of user input via session_waiter."""
        game = self.game_sessions.get(session_id)
        if not game:
            return

        timeout = game["timeout"]

        @session_waiter(timeout=timeout, record_history_chains=False)
        async def guess_song_waiter(
            controller: SessionController,
            event: AstrMessageEvent,
        ):
            if session_id not in self.game_sessions:
                controller.stop()
                return

            game = self.game_sessions[session_id]
            if not game["is_active"]:
                controller.stop()
                return

            self._touch_session_event(session_id, event)
            user_input = self._normalize_user_input(event.message_str)
            user_id = event.get_sender_id()

            if self._is_exit_command(user_input):
                await self._end_game(event, session_id, is_exit=True)
                controller.stop()
                return

            game["round_has_guess"] = True

            current_song = game["song_queue"][game["current_song_index"]]
            correct_answers = current_song.get("answers", [])
            is_correct = any(
                answer.lower() == user_input.lower() for answer in correct_answers
            )

            if is_correct:
                game["no_answer_streak"] = 0

                if user_id not in game["scores"]:
                    game["scores"][user_id] = 0
                game["scores"][user_id] += 1

                correct_msg = random.choice(self.messages["correct"])
                await event.send(
                    event.plain_result(
                        f"{correct_msg} (当前得分: {game['scores'][user_id]})",
                    ),
                )

                game["round"] += 1

                if game["round"] >= game["max_rounds"]:
                    await self._end_game(event, session_id)
                    controller.stop()
                    return

                game["current_song_index"] = (
                    game["current_song_index"] + 1
                ) % len(game["song_queue"])
                game["used_hint"] = False
                game["hint_sent"] = False
                await self._start_round(event, session_id)
                controller.keep(timeout=game["timeout"], reset_timeout=True)
            else:
                # wrong_msg = random.choice(self.messages["wrong"])
                # await event.send(event.plain_result(wrong_msg))

                if self._should_send_half_timeout_hint(controller, game):
                    await self._send_hint(event, session_id)

                controller.keep(timeout=0, reset_timeout=False)

        await guess_song_waiter(anchor_event, session_filter=CustomFilter())

    async def _run_game_session_loop(
        self,
        event: AstrMessageEvent,
        session_id: str,
    ) -> None:
        """Keep session_waiter alive across round timeouts until the game ends."""
        game = self.game_sessions.get(session_id)
        if not game:
            return

        game["anchor_event"] = event

        while session_id in self.game_sessions:
            game = self.game_sessions.get(session_id)
            if not game or not game["is_active"]:
                break

            anchor_event = game.get("anchor_event", event)
            try:
                await self._wait_for_guess_input(anchor_event, session_id)
            except TimeoutError:
                if session_id not in self.game_sessions:
                    break
                game = self.game_sessions.get(session_id)
                if not game or not game["is_active"]:
                    break
                anchor_event = game.get("anchor_event", event)
                await self._handle_timeout(anchor_event, session_id)
                continue
            break

    @filter.command("退出", alias={"猜歌迷退出", "结束游戏"})
    async def exit_guess_song(self, event: AstrMessageEvent):
        """退出当前猜歌迷游戏（需与开始游戏时使用相同的唤醒方式）。"""
        session_id = _game_session_id(event)
        if session_id not in self.game_sessions:
            yield event.plain_result("当前没有进行中的猜歌迷游戏。")
            return
        await self._end_game(event, session_id, is_exit=True)

    @filter.command("猜歌迷", alias={"猜歌", "猜歌曲"})
    async def handle_guess_song(self, event: AstrMessageEvent):
        """开始猜歌迷游戏"""
        self.song_data = self._load_songs_from_config()

        if not self.song_data:
            yield event.plain_result(self.messages["no_songs"])
            return

        session_id = _game_session_id(event)
        if session_id in self.game_sessions:
            yield event.plain_result("⚠️ 当前会话已有进行中的游戏，请先完成或退出！")
            return

        timeout = self.config.get("timeout", 60)
        max_rounds = self.config.get("max_rounds", 10)
        no_answer_auto_end_rounds = self.config.get("no_answer_auto_end_rounds", 2)

        song_queue = self.song_data.copy()
        random.shuffle(song_queue)

        game_data = {
            "scores": {},
            "nicknames": {},
            "round": 0,
            "max_rounds": max_rounds,
            "current_song_index": 0,
            "song_queue": song_queue,
            "timeout": timeout,
            "is_active": True,
            "used_hint": False,
            "hint_sent": False,
            "round_has_guess": False,
            "no_answer_streak": 0,
            "no_answer_auto_end_rounds": no_answer_auto_end_rounds,
            "start_time": asyncio.get_event_loop().time(),
            "anchor_event": event,
        }
        self.game_sessions[session_id] = game_data
        self._remember_player_name(game_data, event.get_sender_id(), event)

        try:
            start_msg = random.choice(self.messages["start"])
            yield event.plain_result(
                f"{start_msg}\n🎯 共 {len(self.song_data)} 首歌曲，进行 {max_rounds} 轮",
            )

            await self._start_round(event, session_id)
            await self._run_game_session_loop(event, session_id)
        except Exception as e:
            logger.error(f"猜歌游戏运行失败: {e}")
            self._stop_session_waiter(session_id)
            self.game_sessions.pop(session_id, None)
            yield event.plain_result("😅 游戏出现了问题，请重新开始")

    async def _start_round(self, event: AstrMessageEvent, session_id: str):
        """开始新一轮游戏"""
        game = self.game_sessions.get(session_id)
        if not game:
            return

        game["anchor_event"] = event
        self._cancel_half_timeout_hint_task(game)
        game["round_has_guess"] = False

        current_song = game["song_queue"][game["current_song_index"]]
        round_msg = self.messages["round_info"].format(
            game["round"] + 1,
            game["max_rounds"],
        )
        await event.send(event.plain_result(round_msg))

        file_path = current_song["file_path"]
        clip_path, duration = self._extract_audio_clip(file_path, duration=5)

        if clip_path:
            sent = await self._send_audio_file(
                event,
                clip_path,
                delete_after_send=True,
            )
            # if sent:
            #     await event.send(
            #         event.plain_result(f"🎵 猜猜这是哪首歌？({duration:.1f}秒片段)"),
            #     )
        else:
            sent = await self._send_audio_file(
                event,
                file_path,
                delete_after_send=False,
            )
            # if sent:
            #     await event.send(
            #         event.plain_result(
            #             "🎵 猜猜这是哪首歌？(未能裁剪片段，已发送完整音频)",
            #         ),
            #     )

        if not sent:
            await event.send(
                event.plain_result(
                    "🎵 音频发送失败，请检查平台是否支持语音消息。",
                ),
            )
            game["round"] += 1
            if game["round"] >= game["max_rounds"]:
                await self._end_game(event, session_id)
                return
            game["current_song_index"] = (
                game["current_song_index"] + 1
            ) % len(game["song_queue"])
            await self._start_round(event, session_id)
            return

        game["used_hint"] = False
        game["hint_sent"] = False
        self._start_half_timeout_hint_task(session_id)

    async def _send_hint(self, event: AstrMessageEvent, session_id: str):
        """发送提示"""
        game = self.game_sessions.get(session_id)
        if not game:
            return

        current_song = game["song_queue"][game["current_song_index"]]
        correct_answers = current_song.get("answers", [])
        correct_answer = correct_answers[0] if correct_answers else "未知"
        wrong_answers = self._get_answer_choices(correct_answers, count=3)

        hint_parts = ["⏰ 时间过了一半啦！给你个提示："]

        if current_song.get("artist"):
            hint_parts.append(
                self.messages["hint"][0].format(current_song["artist"]),
            )
        elif current_song.get("year"):
            hint_parts.append(
                self.messages["hint"][1].format(current_song["year"]),
            )
        elif current_song.get("lyric"):
            hint_parts.append(
                self.messages["hint"][2].format(current_song["lyric"]),
            )
        elif current_song.get("genre"):
            hint_parts.append(
                self.messages["hint"][3].format(current_song["genre"]),
            )
        elif len(correct_answer) > 2:
            hint_parts.append(
                f"📝 答案有 {len(correct_answer)} 个字，开头是 '{correct_answer[0]}'",
            )
        else:
            hint_parts.append(f"📝 答案是 {len(correct_answer)} 个字的歌曲")

        hint_parts.append(f"❌ 这些都不是正确答案哦：{', '.join(wrong_answers)}")

        await event.send(event.plain_result("\n".join(hint_parts)))
        game["hint_sent"] = True
        game["used_hint"] = True

    async def _handle_timeout(self, event: AstrMessageEvent, session_id: str):
        """处理超时"""
        game = self.game_sessions.get(session_id)
        if not game:
            return

        if game.get("round_has_guess"):
            game["no_answer_streak"] = 0
        else:
            game["no_answer_streak"] = game.get("no_answer_streak", 0) + 1

        current_song = game["song_queue"][game["current_song_index"]]
        correct_answers = current_song.get("answers", [])
        correct_answer = correct_answers[0] if correct_answers else "未知"

        timeout_msg = random.choice(self.messages["timeout"]).format(correct_answer)
        await event.send(event.plain_result(timeout_msg))

        auto_end_rounds = game.get("no_answer_auto_end_rounds", 2)
        if auto_end_rounds > 0 and game["no_answer_streak"] >= auto_end_rounds:
            await event.send(
                event.plain_result(
                    self.messages["auto_end_no_answer"].format(auto_end_rounds),
                ),
            )
            await self._end_game(event, session_id)
            return

        game["round"] += 1

        if game["round"] >= game["max_rounds"]:
            await self._end_game(event, session_id)
        else:
            game["current_song_index"] = (
                game["current_song_index"] + 1
            ) % len(game["song_queue"])
            await self._start_round(event, session_id)

    async def _end_game(
        self,
        event: AstrMessageEvent,
        session_id: str,
        is_exit: bool = False,
    ):
        """结束游戏并显示结算"""
        game = self.game_sessions.get(session_id)
        if not game:
            return

        game["is_active"] = False
        self._cancel_half_timeout_hint_task(game)
        self._stop_session_waiter(session_id)

        total_score = sum(game["scores"].values()) if game["scores"] else 0

        if is_exit:
            end_msg = random.choice(self.messages["exit"])
            await event.send(event.plain_result(f"{end_msg} 总得分：{total_score}分"))
        else:
            end_msg = random.choice(self.messages["end"]).format(game["max_rounds"])
            await event.send(event.plain_result(f"{end_msg} 总得分：{total_score}分"))

        await self._send_leaderboard(event, game, is_exit)

        self.game_sessions.pop(session_id, None)
