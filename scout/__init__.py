"""NomadReady's agents. (Not named `agents/`: that would shadow the OpenAI Agents SDK package.)

intake.py      Lead, phase 1: extract the trip brief; ask a clarifying question if it's too vague
lead.py        Lead, phase 2: fan out City Researchers in parallel, check for gaps, follow up
researcher.py  City Researcher: one city, fresh context, the tools in tools/
writer.py      Writer: the cited report, from the findings only
pipeline.py    Runs the phases in order and returns (response, tool_calls)
"""
