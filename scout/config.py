"""Models and the caps that keep an answer well under the graders' 5-minute wait."""

from agents import ModelSettings, set_tracing_disabled
from agents.extensions.models.litellm_model import LitellmModel

# Traces upload to OpenAI by default, which needs an OpenAI key. We're on Gemini.
set_tracing_disabled(True)

# LiteLLM routes to Gemini on Vertex AI. api_key is unused: Vertex uses gcloud credentials.
MODEL = LitellmModel(model="vertex_ai/gemini-3.5-flash", api_key="unused")
# Researchers copy many numbers into their findings. Flash-lite garbled counts in testing
# (1,121 for a tool's 1,799), so they use the full model too.
RESEARCHER_MODEL = LitellmModel(model="vertex_ai/gemini-3.5-flash", api_key="unused")
# A stalled model call would hang the request: give up after 90 s, retry twice.
SETTINGS = ModelSettings(extra_args={"vertex_location": "global", "timeout": 90, "num_retries": 2})

MAX_RESEARCHERS = 10      # research_city calls per question: ~4 initial + follow-ups
MAX_ROUNDS = 3            # Lead turns that spawn researchers: 1 fan-out + 2 follow-up rounds
MAX_COWORKING = 8         # Google Places searches per question (console quota is the backstop)
MAX_QUERIES_PER_RESEARCHER = 3  # custom SQL calls; one researcher spent all 12 turns on them in testing
RESEARCH_DEADLINE_S = 120  # no new researchers after this; a follow-up takes ~30-55 s, then the Writer
LEAD_MAX_TURNS = 10
RESEARCHER_MAX_TURNS = 12
