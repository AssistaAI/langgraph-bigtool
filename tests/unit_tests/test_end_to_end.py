import math
import types
import uuid
from typing import Callable

import pytest
from langchain_core.embeddings import Embeddings
from langchain_core.embeddings.fake import DeterministicFakeEmbedding
from langchain_core.language_models import GenericFakeChatModel, LanguageModelLike
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt import InjectedStore
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore
from typing_extensions import Annotated

from langgraph_bigtool import create_agent
from langgraph_bigtool.graph import State
from langgraph_bigtool.utils import convert_positional_only_function_to_tool

EMBEDDING_SIZE = 1536


# Create a list of all the functions in the math module
all_names = dir(math)

math_functions = [
    getattr(math, name)
    for name in all_names
    if isinstance(getattr(math, name), types.BuiltinFunctionType)
]

# Convert to tools, handling positional-only arguments (idiosyncrasy of math module)
all_tools = []
for function in math_functions:
    if wrapper := convert_positional_only_function_to_tool(function):
        all_tools.append(wrapper)

# Store tool objects in registry
tool_registry = {str(uuid.uuid4()): tool for tool in all_tools}


class FakeModel(GenericFakeChatModel):
    def bind_tools(self, *args, **kwargs) -> "FakeModel":
        """Do nothing for now."""
        return self


def _get_fake_llm_and_embeddings():
    fake_embeddings = DeterministicFakeEmbedding(size=EMBEDDING_SIZE)

    acos_tool = next(tool for tool in tool_registry.values() if tool.name == "acos")
    initial_query = (
        f"{acos_tool.name}: {acos_tool.description}"  # make same as embedding
    )
    fake_llm = FakeModel(
        messages=iter(
            [
                AIMessage(
                    "",
                    tool_calls=[
                        {
                            "name": "retrieve_tools",
                            "args": {"query": initial_query},
                            "id": "abc123",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(
                    "",
                    tool_calls=[
                        {
                            "name": "acos",
                            "args": {"x": 0.5},
                            "id": "abc234",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage("The arc cosine of 0.5 is approximately 1.047 radians."),
            ]
        )
    )

    return fake_llm, fake_embeddings


def _validate_result(result: State) -> None:
    assert set(result.keys()) == {"messages", "selected_tool_ids"}
    assert "acos" in [
        tool_registry[tool_id].name for tool_id in result["selected_tool_ids"]
    ]
    assert set(message.type for message in result["messages"]) == {
        "human",
        "ai",
        "tool",
    }
    tool_calls = [
        tool_call
        for message in result["messages"]
        if isinstance(message, AIMessage)
        for tool_call in message.tool_calls
    ]
    assert tool_calls
    tool_call_names = [tool_call["name"] for tool_call in tool_calls]
    assert "retrieve_tools" in tool_call_names
    math_tool_calls = [
        tool_call for tool_call in tool_calls if tool_call["name"] == "acos"
    ]
    assert len(math_tool_calls) == 1
    math_tool_call = math_tool_calls[0]
    tool_messages = [
        message
        for message in result["messages"]
        if isinstance(message, ToolMessage)
        and message.tool_call_id == math_tool_call["id"]
    ]
    assert len(tool_messages) == 1
    tool_message = tool_messages[0]
    assert round(float(tool_message.content), 4) == 1.0472
    reply = result["messages"][-1]
    assert isinstance(reply, AIMessage)
    assert not reply.tool_calls
    assert reply.content


def run_end_to_end_test(
    llm: LanguageModelLike,
    embeddings: Embeddings,
    retrieve_tools_function: Callable | None = None,
    retrieve_tools_coroutine: Callable | None = None,
) -> None:
    # Store tool descriptions in store
    store = InMemoryStore(
        index={
            "embed": embeddings,
            "dims": EMBEDDING_SIZE,
            "fields": ["description"],
        }
    )
    for tool_id, tool in tool_registry.items():
        store.put(
            ("tools",),
            tool_id,
            {
                "description": f"{tool.name}: {tool.description}",
            },
        )

    builder = create_agent(
        llm,
        tool_registry,
        retrieve_tools_function=retrieve_tools_function,
        retrieve_tools_coroutine=retrieve_tools_coroutine,
    )
    agent = builder.compile(store=store)

    result = agent.invoke(
        {"messages": "Use available tools to calculate arc cosine of 0.5."}
    )
    _validate_result(result)


async def run_end_to_end_test_async(
    llm: LanguageModelLike,
    embeddings: Embeddings,
    retrieve_tools_function: Callable | None = None,
    retrieve_tools_coroutine: Callable | None = None,
) -> None:
    # Store tool descriptions in store
    store = InMemoryStore(
        index={
            "embed": embeddings,
            "dims": EMBEDDING_SIZE,
            "fields": ["description"],
        }
    )
    for tool_id, tool in tool_registry.items():
        await store.aput(
            ("tools",),
            tool_id,
            {
                "description": f"{tool.name}: {tool.description}",
            },
        )

    builder = create_agent(
        llm,
        tool_registry,
        retrieve_tools_function=retrieve_tools_function,
        retrieve_tools_coroutine=retrieve_tools_coroutine,
    )
    agent = builder.compile(store=store)

    result = await agent.ainvoke(
        {"messages": "Use available tools to calculate arc cosine of 0.5."}
    )
    _validate_result(result)


def custom_retrieve_tools(
    query: str,
    *,
    store: Annotated[BaseStore, InjectedStore],
) -> list[str]:
    raise AssertionError


async def acustom_retrieve_tools(
    query: str,
    *,
    store: Annotated[BaseStore, InjectedStore],
) -> list[str]:
    raise AssertionError


def test_end_to_end() -> None:
    # Default
    fake_llm, fake_embeddings = _get_fake_llm_and_embeddings()
    run_end_to_end_test(fake_llm, fake_embeddings)

    # Custom
    fake_llm, fake_embeddings = _get_fake_llm_and_embeddings()
    with pytest.raises(TypeError):
        # No sync function provided
        run_end_to_end_test(
            fake_llm,
            fake_embeddings,
            retrieve_tools_coroutine=acustom_retrieve_tools,
        )

    fake_llm, fake_embeddings = _get_fake_llm_and_embeddings()
    with pytest.raises(AssertionError):
        # Calls custom sync function
        run_end_to_end_test(
            fake_llm,
            fake_embeddings,
            retrieve_tools_function=custom_retrieve_tools,
            retrieve_tools_coroutine=acustom_retrieve_tools,
        )

    fake_llm, fake_embeddings = _get_fake_llm_and_embeddings()
    with pytest.raises(AssertionError):
        # Calls custom sync function
        run_end_to_end_test(
            fake_llm,
            fake_embeddings,
            retrieve_tools_function=custom_retrieve_tools,
        )


async def test_end_to_end_async() -> None:
    # Default
    fake_llm, fake_embeddings = _get_fake_llm_and_embeddings()
    await run_end_to_end_test_async(fake_llm, fake_embeddings)

    # Custom
    fake_llm, fake_embeddings = _get_fake_llm_and_embeddings()
    with pytest.raises(AssertionError):
        # Calls custom sync function
        await run_end_to_end_test_async(
            fake_llm,
            fake_embeddings,
            retrieve_tools_function=custom_retrieve_tools,
        )

    fake_llm, fake_embeddings = _get_fake_llm_and_embeddings()
    with pytest.raises(AssertionError):
        # Calls custom sync function
        await run_end_to_end_test_async(
            fake_llm,
            fake_embeddings,
            retrieve_tools_function=custom_retrieve_tools,
            retrieve_tools_coroutine=acustom_retrieve_tools,
        )

    fake_llm, fake_embeddings = _get_fake_llm_and_embeddings()
    with pytest.raises(AssertionError):
        # Calls custom sync function
        await run_end_to_end_test_async(
            fake_llm,
            fake_embeddings,
            retrieve_tools_coroutine=acustom_retrieve_tools,
        )
