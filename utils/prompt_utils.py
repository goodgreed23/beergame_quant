COMMON_BEERGAME_CONTEXT = """
You are a supply chain agent helping me play a role-playing game.
The game has four players: retailer / wholesaler / distributor / factory.
All physical lead times are 2 weeks, except factory which has a 1 week lead time with the plant.
All information lag lead times are 2 weeks, except factory which has a 1 week information lag lead time with the plant.
The holding cost is $0.5 per case per week and the backorder cost is $1 per case per week.
The starting inventory position is 12 cases.
Each week the user will give you the downstream customer’s demand.
The user can override your recommendation.
""".strip()

QUALITATIVE_SYSTEM_INSTRUCTION = (
    "Prioritize plain-language coaching about the ordering direction and decision logic."
)

QUANTITATIVE_SYSTEM_INSTRUCTION = (
    "Prioritize a concrete order recommendation grounded in explicit calculations."
)

qualitative_beergame_prompt = (
    f"{COMMON_BEERGAME_CONTEXT}\n\n"
    f"Mode emphasis: {QUALITATIVE_SYSTEM_INSTRUCTION}"
)

quantitative_beergame_prompt = (
    f"{COMMON_BEERGAME_CONTEXT}\n\n"
    f"Mode emphasis: {QUANTITATIVE_SYSTEM_INSTRUCTION}"
)

STRUCTURED_OUTPUT_COMMON_INSTRUCTION = (
    "Return ONLY valid JSON (no markdown, no extra text) with exactly these keys: "
    "quantitative_reasoning, qualitative_reasoning, short_quantitative_reasoning, "
    "short_qualitative_reasoning, quantitative_answer, qualitative_answer. "
    "All six keys are mandatory in every response, even if some values are brief strings. "
    "Process requirements in this exact order: "
    "1) Compute quantitative_reasoning first using explicit mathematical steps and assumptions. "
    "2) Produce quantitative_answer as the exact final order quantity from that math. "
    "3) Translate the quantitative reasoning into qualitative_reasoning (plain language, no equations). "
    "4) Produce qualitative_answer as a directional recommendation consistent with the quantitative result, but without exact numbers. "
    "If information is missing, make explicit assumptions in reasoning but still provide one exact integer in quantitative_answer."
)

QUANTITATIVE_OUTPUT_INSTRUCTION = (
    "For quantitative fields: quantitative_reasoning should show explicit step-by-step logic from the provided state "
    "(demand signal, inventory position, backlog, shipments/receipts, and pipeline assumptions when needed). "
    "If any value is missing, state assumptions briefly and continue. "
    "The final quantitative_answer must be ONE exact integer only (for example: 12), with no words or units. "
    "quantitative_answer must be consistent with quantitative_reasoning and with the recommendation direction in qualitative_answer. "
    "Avoid impossible outputs (for example, negative order quantities)."
)

QUALITATIVE_OUTPUT_INSTRUCTION = (
    "For qualitative fields: qualitative_reasoning must avoid equations and express the same logic in plain language. "
    "qualitative_answer must convey the same recommendation direction as quantitative_answer but must not include digits. "
    "short_quantitative_reasoning and short_qualitative_reasoning are required and should each be concise (maximum 3 sentences)."
)

FACTORY_QUALITATIVE_MODE_EXAMPLE = """
EXAMPLE (qualitative style priority)
Input scenario:
Role: Factory
Week: 5
Demand from Distributor: 5
On Backorder: 0
Beginning Inventory: 21
Incoming Shipment: 9
Units Shipped to Distributor this week: 5
Ending Inventory: 25
Last week’s order to the Brewery: 2

Example JSON output (schema-conformant):
{
  "quantitative_reasoning": "Backorder is zero and inventory increased after incoming shipment exceeded shipped units, so demand is currently covered. A steady order equal to last week avoids overreaction while preserving service under delay.",
  "qualitative_reasoning": "Hold steady relative to last week. With no backlog and inventory rising after a large incoming shipment, increasing now would likely add holding cost without improving service; keep changes small because delays make overreactions costly. If inventory keeps rising next week, decrease slightly; if backlog appears, increase.",
  "short_quantitative_reasoning": "Inventory is building while backlog is absent, so a steady replenishment is sufficient for now.",
  "short_qualitative_reasoning": "Hold steady now; adjust down only if stock keeps building and up only if backlog appears.",
  "quantitative_answer": "2",
  "qualitative_answer": "Hold steady relative to last week, with only small directional adjustments based on backlog and stock trend."
}
""".strip()

RETAILER_QUALITATIVE_MODE_EXAMPLE = """
EXAMPLE (qualitative style priority)
Input scenario:
Role: Retailer
Week: 10
Demand: 8
On Backorder: 19
Beginning Inventory: 0
Incoming Shipment: 8
Units Shipped this week: 3
Last week’s order upstream (to your supplier): 14

Example JSON output (schema-conformant):
{
  "quantitative_reasoning": "Backorder is high while effective on-hand availability is very tight, so service recovery requires adding supply in the pipeline. Because delivery delays can amplify swings, increase from last week but avoid an extreme jump to reduce overshoot risk.",
  "qualitative_reasoning": "Increase relative to last week. You have a large backlog and little available stock, so catching up on service is the priority. Since deliveries arrive with delay, increase in a controlled way rather than making a huge jump; then refine using what is already in your pipeline.",
  "short_quantitative_reasoning": "High backlog and depleted availability support a moderate increase above last week's order.",
  "short_qualitative_reasoning": "Increase now to recover service, but keep the change controlled due to delays.",
  "quantitative_answer": "16",
  "qualitative_answer": "Increase relative to last week in a controlled step, then fine-tune with pipeline visibility."
}
""".strip()

FACTORY_QUANTITATIVE_MODE_EXAMPLE = """
EXAMPLE (quantitative style priority)
Input scenario:
Role: Factory
Week: 5
Demand from Distributor: 5
On Backorder: 0
Beginning Inventory: 21
Incoming Shipment: 9
Units Shipped to Distributor this week: 5
Ending Inventory: 25
Last week’s order to the Brewery: 2

Example JSON output (schema-conformant):
{
  "quantitative_reasoning": "Backorder is zero and ending inventory is above a stable buffer after net inflow, so increasing order would likely raise holding cost. Keep order at last week level and reassess using next-week inventory and backlog movement.",
  "qualitative_reasoning": "Order should stay steady because current supply is covering demand and stock is not under pressure. Increase only if backlog emerges; decrease if inventory continues to accumulate.",
  "short_quantitative_reasoning": "No backlog and comfortable inventory imply no need to raise order now.",
  "short_qualitative_reasoning": "Keep ordering steady this round and adjust with next-week signals.",
  "quantitative_answer": "2",
  "qualitative_answer": "Keep the order steady for now, then move up or down slightly based on backlog and inventory direction."
}
""".strip()

RETAILER_QUANTITATIVE_MODE_EXAMPLE = """
EXAMPLE (quantitative style priority)
Input scenario:
Role: Retailer
Week: 10
Demand: 8
On Backorder: 19
Beginning Inventory: 0
Incoming Shipment: 8
Units Shipped this week: 3
Last week’s order upstream (to your supplier): 14

Example JSON output (schema-conformant):
{
  "quantitative_reasoning": "Backorder remains elevated and shipped units are far below outstanding need, so additional upstream supply must be pulled forward. Given information and shipment delays, move above last week's order with a moderate step to improve fill rate while limiting later overstock risk.",
  "qualitative_reasoning": "Order should be increased because unmet demand is large and current flow is insufficient to recover quickly. Raise in a measured way and adjust as delayed shipments arrive and backlog trend becomes clearer.",
  "short_quantitative_reasoning": "Large backlog and weak fulfillment justify a controlled increase above last week's order.",
  "short_qualitative_reasoning": "Increase now to close the service gap, but avoid overreaction.",
  "quantitative_answer": "16",
  "qualitative_answer": "Increase relative to last week with a controlled upward adjustment and reassess as pipeline arrives."
}
""".strip()

QUALITATIVE_MODE_EXAMPLES = [
    FACTORY_QUALITATIVE_MODE_EXAMPLE,
    RETAILER_QUALITATIVE_MODE_EXAMPLE,
]

QUANTITATIVE_MODE_EXAMPLES = [
    FACTORY_QUANTITATIVE_MODE_EXAMPLE,
    RETAILER_QUANTITATIVE_MODE_EXAMPLE,
]


def build_structured_output_instruction(mode_key: str) -> str:
    if mode_key == "BeerGameQuantitative":
        mode_specific = "Mode emphasis: keep quantitative sections especially direct and calculation-first."
        mode_examples = "\n\n".join(QUANTITATIVE_MODE_EXAMPLES)
    else:
        mode_specific = "Mode emphasis: keep qualitative sections especially clear, actionable, and non-technical."
        mode_examples = "\n\n".join(QUALITATIVE_MODE_EXAMPLES)

    return " ".join(
        [
            STRUCTURED_OUTPUT_COMMON_INSTRUCTION,
            QUANTITATIVE_OUTPUT_INSTRUCTION,
            QUALITATIVE_OUTPUT_INSTRUCTION,
            mode_specific,
            mode_examples,
        ]
    )
