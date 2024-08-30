from typing import Any, List, Optional

from llama_index.core.llms import ChatMessage, ChatResponse
from llama_index.core.llms.function_calling import FunctionCallingLLM
from llama_index.core.memory import ChatMemoryBuffer
from llama_index.core.settings import Settings
from llama_index.core.tools import ToolOutput, ToolSelection
from llama_index.core.tools.types import BaseTool
from llama_index.core.workflow import (
    Context,
    Event,
    StartEvent,
    StopEvent,
    Workflow,
    step,
)
from pydantic import BaseModel


class InputEvent(Event):
    input: list[ChatMessage]


class ToolCallEvent(Event):
    tool_calls: list[ToolSelection]


class AgentRunResult(BaseModel):
    response: ChatResponse
    sources: list[ToolOutput]


class FunctionCallingAgent(Workflow):
    def __init__(
        self,
        *args: Any,
        llm: FunctionCallingLLM | None = None,
        tools: List[BaseTool] | None = None,
        system_prompt: str | None = None,
        verbose: bool = False,
        timeout: float = 360.0,
        name: str,
        role: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, verbose=verbose, timeout=timeout, **kwargs)
        self.tools = tools or []
        self.name = name
        self.role = role

        if llm is None:
            llm = Settings.llm
        self.llm = llm
        assert self.llm.metadata.is_function_calling_model

        self.system_prompt = system_prompt

        self.memory = ChatMemoryBuffer.from_defaults(llm=self.llm)
        self.sources = []

    @step()
    async def prepare_chat_history(self, ctx: Context, ev: StartEvent) -> InputEvent:
        # clear sources
        self.sources = []

        # set system prompt
        if self.system_prompt is not None:
            system_msg = ChatMessage(role="system", content=self.system_prompt)
            self.memory.put(system_msg)

        # get user input
        user_input = ev.input
        user_msg = ChatMessage(role="user", content=user_input)
        self.memory.put(user_msg)
        ctx.session.write_event_to_stream(
            Event(msg=f"[{self.name}] Start to work on: {user_input}")
        )

        # get chat history
        chat_history = self.memory.get()
        return InputEvent(input=chat_history)

    @step()
    async def handle_llm_input(
        self, ctx: Context, ev: InputEvent
    ) -> ToolCallEvent | StopEvent:
        chat_history = ev.input

        response = await self.llm.achat_with_tools(
            self.tools, chat_history=chat_history
        )
        self.memory.put(response.message)

        tool_calls = self.llm.get_tool_calls_from_response(
            response, error_on_no_tool_call=False
        )

        if not tool_calls:
            ctx.session.write_event_to_stream(Event(msg=f"[{self.name}] Finished task"))
            return StopEvent(
                result=AgentRunResult(response=response, sources=[*self.sources])
            )
        else:
            return ToolCallEvent(tool_calls=tool_calls)

    @step()
    async def handle_tool_calls(self, ctx: Context, ev: ToolCallEvent) -> InputEvent:
        tool_calls = ev.tool_calls
        tools_by_name = {tool.metadata.get_name(): tool for tool in self.tools}

        tool_msgs = []

        # call tools -- safely!
        for tool_call in tool_calls:
            tool = tools_by_name.get(tool_call.tool_name)
            additional_kwargs = {
                "tool_call_id": tool_call.tool_id,
                "name": tool.metadata.get_name(),
            }
            if not tool:
                tool_msgs.append(
                    ChatMessage(
                        role="tool",
                        content=f"Tool {tool_call.tool_name} does not exist",
                        additional_kwargs=additional_kwargs,
                    )
                )
                continue

            try:
                tool_output = await tool.acall(**tool_call.tool_kwargs)
                self.sources.append(tool_output)
                tool_msgs.append(
                    ChatMessage(
                        role="tool",
                        content=tool_output.content,
                        additional_kwargs=additional_kwargs,
                    )
                )
            except Exception as e:
                tool_msgs.append(
                    ChatMessage(
                        role="tool",
                        content=f"Encountered error in tool call: {e}",
                        additional_kwargs=additional_kwargs,
                    )
                )

        for msg in tool_msgs:
            self.memory.put(msg)

        chat_history = self.memory.get()
        return InputEvent(input=chat_history)
