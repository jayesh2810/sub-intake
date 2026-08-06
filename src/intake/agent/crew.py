"""Extraction crew.

One agent, one task. Everything after extraction - class resolution, appetite,
clearance, scoring, routing - is deterministic and runs without a model, so it
does not belong here.
"""

import os

from crewai import LLM, Agent, Crew, Process, Task
from crewai.project import CrewBase, agent, crew, task

from intake.agent.tools import ALL_TOOLS
from intake.models import SubmissionExtract

# LiteLLM resolves `gemini/` models against GEMINI_API_KEY. Accept the common
# alternative variable names and normalise onto that.
for _candidate in ("GEMINI_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY", "GOOGLE_API_KEY"):
    if os.environ.get(_candidate):
        os.environ["GEMINI_API_KEY"] = os.environ[_candidate]
        break

# flash-lite by default: the per-day free tier cap on 2.5-flash is 20 requests,
# which is roughly four submissions.
MODEL = os.environ.get("TRIAGE_MODEL", "gemini/gemini-2.5-flash-lite")
MAX_RPM = int(os.environ.get("TRIAGE_MAX_RPM", "8"))


@CrewBase
class ExtractionCrew:
    """Reads a parsed submission packet and returns structured facts."""

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    def _llm(self) -> LLM:
        # Extraction must be reproducible across runs.
        return LLM(model=MODEL, temperature=0.0)

    @agent
    def submission_intake_analyst(self) -> Agent:
        return Agent(
            config=self.agents_config["submission_intake_analyst"],
            llm=self._llm(),
            tools=ALL_TOOLS,
            verbose=False,
        )

    @task
    def extract_submission(self) -> Task:
        return Task(
            config=self.tasks_config["extract_submission"],
            output_pydantic=SubmissionExtract,
        )

    @crew
    def crew(self) -> Crew:
        return Crew(
            agents=self.agents,
            tasks=self.tasks,
            process=Process.sequential,
            max_rpm=MAX_RPM,
            verbose=False,
        )
