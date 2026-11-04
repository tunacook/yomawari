"""本文の文脈から、どれを先に試すかを AI（ローカルの Ollama）に聞く（DESIGN.md §9「文脈から選ぶ」）。

人が質問を読んで「屋上の話をしていたから、屋上の言葉だろう」と見当をつけるように、流れの手順で選ぶもの（言葉・
思い出・選択肢）の文字と、質問の前の本文・流れの途中で読んだ本文をモデルに渡し、候補ごとに 0〜10 の点数を付けさせる。
答えは、まだ試していない候補の中で先に試す順番にだけ使う（外れたら流れの試行錯誤で次へ進む。正解・不正解の判定や
不具合の判定には使わない）。問い合わせは裏のスレッドで行い、答えが出るまでは点数なしとして扱う（操作を止めない）。
"""

from __future__ import annotations

import concurrent.futures
from collections.abc import Callable
from typing import Any

from ..judge.ollama import OllamaError, chat_json

SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "reason": {"type": "string"}, "score": {"type": "integer"}},
                "required": ["id", "reason", "score"],
            },
        }
    },
    "required": ["scores"],
}

Ask = Callable[[str, dict[str, Any]], dict[str, Any]]


def build_prompt(question: list[str], read: list[str], options: list[str]) -> str:
    lines = ["ノベルゲームで、登場人物に質問された（または選ぶ場面になった）。プレイヤーは候補の中から1つを選んで答える。",
             "本文（古い順。最後が質問）:"]
    lines += [f"- {t}" for t in question] or ["- （なし）"]
    if read:
        lines.append("答えるまでに読んだ本文（古い順）:")
        lines += [f"- {t}" for t in read]
    lines.append("候補:")
    lines += [f"- id {i}: {t}" for i, t in enumerate(options)]
    lines.append("候補ごとに、質問の答えとして本文に合っているかを考え、理由と 0〜10 の点数を付けて。")
    return "\n".join(lines)


class ChoiceAdvisor:
    def __init__(self, ask: Ask, model: str) -> None:
        self.ask = ask
        self.model = model
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="advisor")
        self._futures: dict[tuple[Any, ...], concurrent.futures.Future[dict[str, float]]] = {}
        self.asked = 0
        self.answered = 0
        self.failed = 0
        self.last_error: str | None = None
        self.examples: list[str] = []  # 「ハーネスの記録」向けの例

    @classmethod
    def from_judge(cls, judge: dict[str, Any], timeout: float) -> ChoiceAdvisor | None:
        if judge.get("kind", "local") != "local":
            return None
        endpoint = str(judge.get("endpoint", "http://localhost:11434"))
        model = str(judge.get("model", "qwen2.5vl:7b"))
        return cls(lambda prompt, schema: chat_json(endpoint, model, prompt, schema, timeout), model)

    def scores(self, key: Any, question: list[str], read: list[str], options: dict[str, str]) -> dict[str, float] | None:
        """options（候補 → 文字）の点数。まだ答えが出ていなければ問い合わせを始めて None。"""
        names = sorted(options)
        k = (key, tuple((n, options[n]) for n in names))
        fut = self._futures.get(k)
        if fut is None:
            self.asked += 1
            self._futures[k] = self._pool.submit(self._run, question, read, names, [options[n] for n in names])
            return None
        if not fut.done():
            return None
        return fut.result()

    def _run(self, question: list[str], read: list[str], names: list[str], texts: list[str]) -> dict[str, float]:
        try:
            out = self.ask(build_prompt(question, read, texts), SCHEMA)
        except OllamaError as e:
            self.failed += 1
            self.last_error = str(e)
            return {}
        result: dict[str, float] = {}
        for item in out.get("scores") or []:
            try:
                i, score = int(item["id"]), float(item["score"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= i < len(names):
                result[names[i]] = max(0.0, min(10.0, score))
        self.answered += 1
        if result and len(self.examples) < 3:
            best = max(result, key=lambda n: result[n])
            self.examples.append(f"「{question[-1] if question else ''}」に「{texts[names.index(best)]}」（{result[best]:.0f} 点）")
        return result

    def summary(self) -> list[str]:
        if not self.asked:
            return []
        line = (f"本文の文脈から、どれを先に試すかを AI（{self.model}）に {self.asked} 回聞いた（答えが出た {self.answered} 回、"
                f"失敗 {self.failed} 回）")
        if self.examples:
            line += "。例: " + " / ".join(self.examples)
        if self.last_error:
            line += f"。最後の失敗: {self.last_error[:120]}"
        return [line]

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
