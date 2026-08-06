"""The crew definition.

Stage 1 is deliberately one agent, one task, no tools, no memory. Every extra
moving part hides what the basics are doing. Layers get added on top of this
file in later stages, not woven into it.
"""

import os

from crewai import LLM, Agent, Crew, Process, Task
from crewai.project import CrewBase, agent, crew, task

from triage.models import SubmissionExtract

# LiteLLM (which CrewAI uses underneath) reads GEMINI_API_KEY for `gemini/`
# models. Your shell exports the key under a different name, so map whichever
# is present onto the name LiteLLM expects.
for _candidate in ("GEMINI_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY", "GOOGLE_API_KEY"):
    if os.environ.get(_candidate):
        os.environ["GEMINI_API_KEY"] = os.environ[_candidate]
        break

MODEL = os.environ.get("TRIAGE_MODEL", "gemini/gemini-2.5-flash")


@CrewBase
class SubmissionTriageCrew:
    """Reads a broker submission packet and returns structured facts."""

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    def _llm(self) -> LLM:
        # temperature=0 because extraction should be reproducible. The same
        # packet must give the same numbers on every run, or nobody downstream
        # can trust it.
        return LLM(model=MODEL, temperature=0.0)

    @agent
    def submission_intake_analyst(self) -> Agent:
        return Agent(
            config=self.agents_config["submission_intake_analyst"],
            llm=self._llm(),
            verbose=True,
        )

    @task
    def extract_submission(self) -> Task:
        return Task(
            config=self.tasks_config["extract_submission"],
            # This is the line that turns a chatty LLM into a component you can
            # build on. CrewAI forces the response into SubmissionExtract and
            # retries the model if it does not validate.
            output_pydantic=SubmissionExtract,
        )

    @crew
    def crew(self) -> Crew:
        return Crew(
            agents=self.agents,
            tasks=self.tasks,
            process=Process.sequential,
            verbose=True,
        )
