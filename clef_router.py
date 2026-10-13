import json
import time

from free_gateway import CLEF_MODEL


class ClefRouter:
	def __init__(self, core, gateway):
		self.core, self.gateway = core, gateway
		self.until = 0
		self.last = {"model": "cloudflare/clef", "decisions": 0, "fallbacks": 0, "choice": "", "reason": "Waiting for eligible tasks."}

	def choose(self, tasks, slots, board):
		locked = [s["task"]["owns"] for s in slots.values() if s.get("task")]
		eligible = [t for t in tasks if t["state"] == "pending" and not any(self.core.overlaps(t["owns"], owns) for owns in locked)]
		if not eligible:
			return None
		eligible.sort(key=lambda t: (not (t.get("candidate") and not t["candidate"].get("needs_work")), not ("repair" in (str(t["title"]) + " " + str(t["brief"])).lower() or t.get("last_error"))))
		eligible = eligible[:4]
		if len(eligible) == 1 or not self.gateway.providers.get("cloudflare") or self.until > time.time():
			return eligible[0]
		criteria = {t["id"]: str(t["title"])[:80] + ". " + str(t["brief"])[:90] for t in eligible}
		state = json.dumps({"goal": "Reconstruct Gungeon from original evidence. Review ready candidates and repair failures first.", "tasks": [{"id": t["id"], "retry": bool(t.get("last_error")), "candidate_ready": bool(t.get("candidate") and not t["candidate"].get("needs_work"))} for t in eligible]})
		body = {"state": state, "questions": {"task": {"type": "choice", "instructions": "Select one task from the allowed list. All listed tasks passed ownership checks.", "criteria": criteria}}}
		code, response, retry = self.gateway.extra_request("cloudflare", CLEF_MODEL, body)
		if code == 200:
			answer = response.get("answers", {}).get("task", {})
			choice = answer.get("choice")
			if choice in criteria:
				self.last.update(decisions=self.last["decisions"] + 1, choice=choice, confidence=answer.get("confidence"), reason="Selected an eligible task.")
				board.event("route", model="cloudflare/clef", item=choice, confidence=answer.get("confidence"), choices=list(criteria))
				return next(t for t in eligible if t["id"] == choice)
		self.until = time.time() + max(90, retry)
		self.last.update(fallbacks=self.last["fallbacks"] + 1, reason="Clef waits. The scheduler uses the first eligible task.")
		board.event("router-fallback", status=code, retry_after=max(90, retry))
		return eligible[0]
