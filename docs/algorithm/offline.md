# Serverless API Request Routing Optimization

## Problem Statement

This is a **multi-provider API request routing optimization problem** where the core objective is to minimize total cost while satisfying various constraints.

### Key Components

1. **Chutes Subscription Service**: Fixed $20/month, provides 5000 free requests/day (any model)
2. **Other Providers**:
   - Some with unified input/output pricing
   - Some with traditional pricing (cheaper input, more expensive output)
3. **Optimization Goal**: Minimize total cost

## Mathematical Formulation

### 1. Offline Optimization (Linear Programming)

If we know all request information for the day (model, prompt length, expected output length), this becomes a standard **Linear Programming** problem.

#### Decision Variables

Let:
- $i \in \{1, 2, ..., N\}$ be the request index
- $j \in \{1, 2, ..., M\}$ be the provider index
- $x_{ij} \in \{0, 1\}$ be a binary variable indicating whether request $i$ is assigned to provider $j$

#### Objective Function

$$
\min \sum_{i=1}^{N} \sum_{j=1}^{M} c_{ij} \cdot x_{ij}
$$

where $c_{ij}$ is the cost of serving request $i$ with provider $j$.

#### Constraints

1. **Assignment Constraint**: Each request must be assigned to exactly one provider
   $$
   \sum_{j=1}^{M} x_{ij} = 1, \quad \forall i \in \{1, ..., N\}
   $$

2. **Chutes Daily Quota**:
   $$
   \sum_{i=1}^{N} x_{i,\text{chutes}} \leq 5000
   $$

3. **Rate Limit Constraints**: For each provider $j$
   $$
   \sum_{i=1}^{N} x_{ij} \leq Q_j
   $$
   where $Q_j$ is the rate limit for provider $j$.

#### Cost Function

For each request-provider pair, the cost $c_{ij}$ is calculated as:

$$
c_{ij} = \begin{cases}
0 & \text{if } j = \text{chutes and within quota} \\
\infty & \text{if } j = \text{chutes and exceeds quota} \\
t_i^{\text{in}} \cdot p_j^{\text{in}} + t_i^{\text{out}} \cdot p_j^{\text{out}} & \text{otherwise}
\end{cases}
$$

where:
- $t_i^{\text{in}}$ = input tokens for request $i$
- $t_i^{\text{out}}$ = output tokens for request $i$
- $p_j^{\text{in}}$ = input token price for provider $j$
- $p_j^{\text{out}}$ = output token price for provider $j$

#### Closed-Form Solution for a Single Subscription Provider

When Chutes is the only subscription provider and every other vendor charges strictly per token with no additional capacity constraints, the ILP collapses into a simple sorting problem:

1. Remove Chutes from the candidate provider list and compute the cheapest paid option for each request
   $$
   d_i = \min_{j \neq \text{chutes}} c_{ij}.
   $$
2. Sort all requests by $d_i$ in descending order. The top 5000 entries are the requests that save the most money when routed through Chutes.
3. Assign those top 5000 requests to Chutes (cost becomes zero) and route the remaining requests to the paid providers chosen in step 1.

This greedy rule is optimal because the objective reduces to selecting up to 5000 requests whose reassignment to Chutes yields the largest cost savings. The approach stays optimal as long as the following assumptions hold:

- Chutes accepts any model with zero marginal cost inside the daily quota.
- Non-Chutes providers have no additional coupling constraints (no RPM, TPM, region, or concurrency caps).
- Costs are linear in token counts with known prices for both input and output sides.

If any of these assumptions break, the full ILP (or a multi-knapsack variant) becomes necessary.
