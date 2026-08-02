"""Crew definition for submission intake and triage."""

import os

from crewai import LLM, Agent, Crew, Process, Task
from crewai.project import CrewBase, agent, crew, task

from intake.models import SubmissionExtract, TriageDecision
from intake.tools import ALL_TOOLS

# LiteLLM resolves `gemini/` models against GEMINI_API_KEY. Accept the common
# alternative variable names and normalise onto that.
for _candidate in ("GEMINI_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY", "GOOGLE_API_KEY"):
    if os.environ.get(_candidate):
        os.environ["GEMINI_API_KEY"] = os.environ[_candidate]
        break

MODEL = os.environ.get("TRIAGE_MODEL", "gemini/gemini-2.5-flash")

# The tool loop issues roughly ten requests per submission. Free tier Gemini
# allows five per minute on 2.5-flash, so throttle by default rather than
# failing partway through a run.
MAX_RPM = int(os.environ.get("TRIAGE_MAX_RPM", "4"))


@CrewBase
class SubmissionTriageCrew:
    """Reads a broker submission packet and returns a triage decision."""

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    def _llm(self) -> LLM:
        # Extraction and appetite reporting must be reproducible across runs.
        return LLM(model=MODEL, temperature=0.0)

    @agent
    def submission_intake_analyst(self) -> Agent:
        return Agent(
            config=self.agents_config["submission_intake_analyst"],
            llm=self._llm(),
            tools=ALL_TOOLS,
            verbose=True,
        )

    @task
    def extract_submission(self) -> Task:
        return Task(
            config=self.tasks_config["extract_submission"],
            output_pydantic=SubmissionExtract,
        )

    @task
    def triage_submission(self) -> Task:
        return Task(
            config=self.tasks_config["triage_submission"],
            output_pydantic=TriageDecision,
        )

    @crew
    def crew(self) -> Crew:
        return Crew(
            agents=self.agents,
            tasks=self.tasks,
            process=Process.sequential,
            max_rpm=MAX_RPM,
            verbose=True,
        )
