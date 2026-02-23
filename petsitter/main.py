"""PetSitter - Main FastAPI Application."""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from petsitter.api.anthropic import (
    anthropic_to_internal,
    create_anthropic_error_response,
    internal_to_anthropic,
)
from petsitter.api.openai import (
    create_openai_error_response,
    internal_to_openai,
    openai_to_internal,
)
from petsitter.backends.ollama import OllamaBackend
from petsitter.config import Config, create_parser, parse_args
from petsitter.logging.metrics import init_logger
from petsitter.models import (
    AnthropicRequest,
    AnthropicResponse,
    ChatRequest,
    ChatResponse,
    HealthResponse,
    OpenAIRequest,
    OpenAIResponse,
)
from petsitter.retry.engine import RetryEngine
from petsitter.skills.loader import load_skills
from petsitter.skills.stack import create_system_message, stack_skills
from petsitter.validators.registry import run_validators


# Global state
config: Config | None = None
backend: OllamaBackend | None = None
retry_engine: RetryEngine | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager."""
    global config, backend, retry_engine

    # Startup
    parser = create_parser()
    args = parser.parse_args(["serve"])  # Default to serve command
    config = Config.from_args(args)

    # Initialize logger
    init_logger(config)

    # Initialize backend
    backend = OllamaBackend(
        base_url=config.ollama_base_url,
        model=config.model,
    )

    # Initialize retry engine
    retry_engine = RetryEngine(
        max_retries=config.max_retries,
        early_fail=config.early_fail,
    )

    yield

    # Shutdown
    if backend:
        await backend.close()


app = FastAPI(
    title="PetSitter",
    description="A lightweight proxy and babysitter for local LLMs",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Health check endpoint."""
    return HealthResponse()


@app.post("/v1/messages", response_model=AnthropicResponse)
async def anthropic_messages(request: AnthropicRequest) -> AnthropicResponse:
    """Anthropic-compatible messages endpoint.

    This is the main endpoint for Anthropic-style API clients like Claude Code,
    OpenCode, Cursor, etc.
    """
    if backend is None or retry_engine is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    start_time = time.time()
    request_id = str(uuid.uuid4())[:8]

    try:
        # Convert to internal format
        internal_request = anthropic_to_internal(request)

        # Load skills if specified
        skills = []
        if internal_request.skills:
            skills = load_skills(internal_request.skills)

        # Process with retry loop
        response = await process_with_retry(
            internal_request,
            skills,
            request_id,
        )

        # Convert to Anthropic format
        anthropic_response = internal_to_anthropic(
            content=response.content,
            model=response.model,
            usage=response.usage,
        )

        # Log metrics
        duration_ms = (time.time() - start_time) * 1000
        logger = init_logger(config) if config else None
        if logger:
            logger.end_request(
                request_id=request_id,
                retries=response.retries,
                validators_run=len(response.validator_results),
                validators_passed=sum(1 for v in response.validator_results if v.passed),
                validators_failed=sum(1 for v in response.validator_results if not v.passed),
                duration_ms=duration_ms,
            )

        return anthropic_response

    except Exception as e:
        logger = init_logger(config) if config else None
        if logger:
            logger.error(f"Request {request_id} failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/chat/completions", response_model=OpenAIResponse)
async def openai_chat_completions(request: OpenAIRequest) -> OpenAIResponse:
    """OpenAI-compatible chat completions endpoint."""
    if backend is None or retry_engine is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    start_time = time.time()
    request_id = str(uuid.uuid4())[:8]

    try:
        # Convert to internal format
        internal_request = openai_to_internal(request)

        # Load skills if specified (via extra params or model name parsing)
        skills = []

        # Process with retry loop
        response = await process_with_retry(
            internal_request,
            skills,
            request_id,
        )

        # Convert to OpenAI format
        openai_response = internal_to_openai(
            content=response.content,
            model=response.model,
            usage=response.usage,
        )

        # Log metrics
        duration_ms = (time.time() - start_time) * 1000
        logger = init_logger(config) if config else None
        if logger:
            logger.end_request(
                request_id=request_id,
                retries=response.retries,
                validators_run=len(response.validator_results),
                validators_passed=sum(1 for v in response.validator_results if v.passed),
                validators_failed=sum(1 for v in response.validator_results if not v.passed),
                duration_ms=duration_ms,
            )

        return openai_response

    except Exception as e:
        logger = init_logger(config) if config else None
        if logger:
            logger.error(f"Request {request_id} failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def process_with_retry(
    request: ChatRequest,
    skills: list,
    request_id: str,
) -> ChatResponse:
    """Process a request with retry logic.

    Args:
        request: Internal chat request
        skills: List of loaded skills
        request_id: Unique request identifier

    Returns:
        ChatResponse with validated content
    """
    global backend, retry_engine

    if backend is None or retry_engine is None:
        raise RuntimeError("Backend or retry engine not initialized")

    # Create retry state
    state = retry_engine.create_state()

    # Stack skills if any
    stacked_skills = stack_skills(skills) if skills else None

    # Determine model to use
    model = request.model
    if stacked_skills and stacked_skills.model_pin:
        model = stacked_skills.model_pin

    # Prepare messages with skill system prompt
    messages = request.messages.copy()
    if stacked_skills:
        system_prompt = create_system_message(stacked_skills)
        # Insert or prepend system prompt
        if messages and messages[0].role.value == "system":
            messages[0].content = system_prompt + "\n\n" + messages[0].content
        else:
            messages.insert(0, Message(role="system", content=system_prompt))

    last_response = ""
    validator_results = []

    while retry_engine.should_retry(state):
        retry_engine.record_attempt(state)

        # Prepare messages for this attempt
        if state.attempt > 0 and last_response:
            messages = retry_engine.create_retry_messages(
                request.messages,
                state,
                last_response,
            )
            # Re-apply skill system prompt
            if stacked_skills:
                system_prompt = create_system_message(stacked_skills)
                if messages and messages[0].role.value == "system":
                    messages[0].content = system_prompt + "\n\n" + messages[0].content
                else:
                    messages.insert(0, Message(role="system", content=system_prompt))

        # Call backend
        chat_request = ChatRequest(
            messages=messages,
            model=model,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
        )

        response = await backend.chat(chat_request)
        last_response = response.content

        # Run validators if we have any
        if stacked_skills and stacked_skills.validators:
            # Extract code blocks from response
            from petsitter.validators.base import extract_python_code_blocks

            code_blocks = extract_python_code_blocks(response.content)

            if code_blocks:
                # Run validators on each code block
                for code in code_blocks:
                    results = run_validators(stacked_skills.validators, code, response.content)
                    validator_results.extend(results)

                    # Check for early fail
                    if retry_engine.should_early_fail(results):
                        all_passed = False
                        break
                else:
                    all_passed = all(r.passed for r in results)
            else:
                # No code to validate, pass by default
                all_passed = True
        else:
            all_passed = True

        # Check if all validators passed
        if all_passed:
            return ChatResponse(
                content=response.content,
                model=model,
                usage=response.usage,
                retries=state.attempt,
                validator_results=validator_results,
            )

        # Process failures and prepare for retry
        all_passed, feedback = retry_engine.process_validator_results(state, validator_results)

        if not all_passed and not retry_engine.should_retry(state):
            # Max retries exceeded
            break

    # Max retries exceeded or still failing
    final_response = ChatResponse(
        content=last_response,
        model=model,
        retries=state.attempt,
        validator_results=validator_results,
    )

    return final_response


def cli() -> None:
    """CLI entry point."""
    import sys

    from petsitter.logging.metrics import init_logger

    args = parse_args()

    if args.command is None or args.command == "serve":
        # Load config
        if args.command and hasattr(args, "config") and args.config:
            from pathlib import Path

            config = Config.from_yaml(Path(args.config))
        else:
            config = Config.from_args(args)

        # Initialize logger
        init_logger(config)

        # Run server
        import uvicorn

        uvicorn.run(
            "petsitter.main:app",
            host=config.host,
            port=config.port,
            reload=False,
        )
    elif args.command == "search":
        print("Skill search not yet implemented")
        sys.exit(0)
    else:
        print(f"Unknown command: {args.command}")
        sys.exit(1)


if __name__ == "__main__":
    cli()
