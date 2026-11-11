"""Models and the caps that keep an answer well under the graders' 5-minute wait."""

import dataclasses
import logging

import litellm
from agents import ModelSettings, set_tracing_disabled
from agents.extensions.models.litellm_model import LitellmModel

# Traces upload to OpenAI by default, which needs an OpenAI key. We're on Gemini.
set_tracing_disabled(True)
litellm.suppress_debug_info = True

log = logging.getLogger("nomadready")

# Vertex AI endpoints for Gemini 3.5 Flash. It isn't served from single regions (us-central1 etc.
# return 404); `global` and the `us` / `eu` multi-regions do. `global` runs on capacity shared by
# all customers and returned 429s ("Resource exhausted") in testing, so a call that fails there
# is retried on `us`.
PRIMARY_LOCATION = "global"
FALLBACK_LOCATION = "us"
FALLBACK_ON = (litellm.RateLimitError, litellm.ServiceUnavailableError, litellm.InternalServerError,
               litellm.Timeout, litellm.APIConnectionError)


class FallbackModel(LitellmModel):
    """LiteLLM model that retries a failed call once on the fallback Vertex location."""

    async def get_response(self, system_instructions, input, model_settings, *args, **kwargs):
        try:
            return await super().get_response(system_instructions, input, model_settings, *args, **kwargs)
        except FALLBACK_ON as e:
            log.warning("model call failed on %s (%s); retrying on %s", PRIMARY_LOCATION, type(e).__name__,
                        FALLBACK_LOCATION)
            moved = dataclasses.replace(model_settings, extra_args={
                **(model_settings.extra_args or {}), "vertex_location": FALLBACK_LOCATION, "num_retries": 2})
            return await super().get_response(system_instructions, input, moved, *args, **kwargs)


# LiteLLM routes to Gemini on Vertex AI. api_key is unused: Vertex uses gcloud credentials.
MODEL = FallbackModel(model="vertex_ai/gemini-3.5-flash", api_key="unused")
# Researchers copy many numbers into their findings. Flash-lite garbled counts in testing
# (1,121 for a tool's 1,799), so they use the full model too.
RESEARCHER_MODEL = FallbackModel(model="vertex_ai/gemini-3.5-flash", api_key="unused")
# A stalled model call would hang the request: give up after 90 s. One retry on `global`, then
# FallbackModel moves the call to `us` (with two more retries there).
SETTINGS = ModelSettings(extra_args={"vertex_location": PRIMARY_LOCATION, "timeout": 90, "num_retries": 1})

MAX_RESEARCHERS = 10      # research_city calls per question: ~4 initial + follow-ups
MAX_ROUNDS = 3            # Lead turns that spawn researchers: 1 fan-out + 2 follow-up rounds
MAX_COWORKING = 8         # Google Places searches per question (console quota is the backstop)
MAX_QUERIES_PER_RESEARCHER = 3  # custom SQL calls; one researcher spent all 12 turns on them in testing
RESEARCH_DEADLINE_S = 120  # no new researchers after this; a follow-up takes ~30-55 s, then the Writer
LEAD_MAX_TURNS = 10
RESEARCHER_MAX_TURNS = 12

# Wall-clock limits. One stalled model call can take 90 s x 3 tries, and in testing a run hung past
# 6 minutes, so each stage is capped and a late stage degrades instead of blowing the 5 minutes.
RESEARCHER_TIMEOUT_S = 100     # a researcher still running after this becomes a gap
RESEARCH_PHASE_TIMEOUT_S = 180  # then the Writer works from whatever findings exist
WRITER_DEADLINE_S = 285        # the draft must finish by this point in the run, or a plain summary is sent
REVISION_CUTOFF_S = 220        # skip the Verifier's model check and the revision after this
