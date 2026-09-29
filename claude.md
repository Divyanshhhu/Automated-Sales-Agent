# Project Engineering Guidelines

## Architecture
- Design for scalability without over-engineering for hypothetical scale.
- Keep frontend, backend, database, external services, and business logic modular.
- Prefer simple, production-appropriate architecture.
- Avoid unnecessary agents, services, dependencies, or abstractions.

## Code Quality
- Write clean, typed, modular and maintainable code.
- Avoid quick hacks and duplicated logic.
- Keep components/functions focused on a single responsibility.
- Reuse existing utilities before creating new ones.

## Scalability & Reliability
- Prefer stateless backend services where practical.
- Design database schemas and APIs with future growth and backward compatibility in mind.
- External API calls must handle timeouts, retries, rate limits, and failures appropriately.
- Important operations should be idempotent where retries are possible.

## Security
- Never hardcode API keys, secrets, passwords, or credentials.
- Use environment variables for configuration and secrets.
- Validate external/user input.
- Follow least-privilege principles.

## Observability
- Use structured logging.
- Provide meaningful error handling.
- Keep important operations observable and debuggable.

## Dependencies
- Don't add a dependency unless it solves a real problem.
- Don't introduce major architectural changes without explaining the trade-offs first.

## Development Process
- Understand the existing architecture before modifying it.
- Prefer incremental changes.
- Before implementing a major feature, explain the proposed approach briefly.
- Don't rewrite working code unnecessarily.
- Run relevant tests/typechecks/linting after changes.

## Important
When there are multiple possible approaches, choose the simplest approach
that is production-ready and can scale later.

Do not optimize for hypothetical massive scale at the cost of unnecessary
complexity.
    