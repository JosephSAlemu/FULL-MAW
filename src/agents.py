from academy.agent import Agent, action
from academy.handle import Handle
from academy.exchange import LocalExchangeFactory
from academy.manager import Manager
from langchain_core.tools import Tool

from langchain.tools import tool
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent


class Orchestrator(Agent):
    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        agents: dict[Handle[Agent]]
    ):
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.agents = agents

    async def agent_on_startup(self) -> None:
        llm = ChatOpenAI(
                         model=self.model, 
                         api_key=self.api_key,
                         base_url=self.base_url,
                         streaming=True, 
                         stream_usage=True
                         )
        tools = [
        ]
        self.react_loop = create_agent(llm, tools=tools)