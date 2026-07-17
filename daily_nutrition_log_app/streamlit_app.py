import ast
import io
import json
import re
import uuid

import streamlit as st
from PIL import Image
from snowflake.snowpark.context import get_active_session


# =========================================================
# APPLICATION CONFIGURATION
# =========================================================

st.set_page_config(
    page_title="Daily Nutrition Log",
    page_icon="🥗",
    layout="wide"
)

session = get_active_session()

# Display and store timestamps in India time.
session.sql("ALTER SESSION SET TIMEZONE = 'Asia/Kolkata'").collect()

STAGE = "@IMAGE_ANALYSIS.PLATE_NUTRITION.FOODPLATE_IMAGES_STAGE"
TABLE = "IMAGE_ANALYSIS.PLATE_NUTRITION.DAILY_NUTRITION_LOG"
MODEL = "openai-gpt-4.1"


# =========================================================
# SESSION STATE
# =========================================================

if "uploader_key" not in st.session_state:
    st.session_state["uploader_key"] = 0

if st.session_state.pop("reset_meal_form", False):
    st.session_state["meal_type_input"] = "Breakfast"
    st.session_state["user_note_input"] = ""

if st.session_state.pop("meal_saved", False):
    st.success("Meal analyzed and added to today's nutrition log.")


# =========================================================
# PAGE HEADER
# =========================================================

st.title("🥗 AI Daily Nutrition Log")

st.caption(
    "Upload meals throughout the day and track your live calories, "
    "protein, carbohydrates, fat and fiber."
)


# =========================================================
# AI PROMPT
# =========================================================

PROMPT = """
Analyze the uploaded food plate image and identify all clearly visible food items.

Return only valid JSON in this structure:

{
  "cuisine_type": "cuisine type or unknown",
  "image_quality": "good, acceptable, poor or unusable",
  "food_items": [
    {
      "food_name": "identified food item",
      "estimated_quantity": "short visible serving estimate",
      "estimated_quantity_grams": 0,
      "calories": 0,
      "protein_grams": 0,
      "carbohydrates_grams": 0,
      "fat_grams": 0,
      "fiber_grams": 0,
      "confidence": 0.00
    }
  ],
  "total_calories": 0,
  "total_protein_grams": 0,
  "total_carbohydrates_grams": 0,
  "total_fat_grams": 0,
  "total_fiber_grams": 0,
  "overall_assessment": "one short nutritional summary"
}

Rules:
- Identify only clearly visible food items.
- Combine repeated portions of the same food into one item.
- Do not invent hidden ingredients.
- Return no more than 8 food items.
- Estimate quantity and nutrition using only the visible serving.
- Confidence must be between 0 and 1.
- Ensure totals approximately match the sum of all food items.
- Return JSON only without Markdown or code fences.
"""



# =========================================================
# HELPER FUNCTIONS
# =========================================================

def safe_number(value, default=0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def format_number(value):
    number = safe_number(value)

    if number.is_integer():
        return f"{int(number):,}"

    return f"{number:,.1f}"


def format_confidence(value):
    try:
        confidence = max(0, min(1, float(value)))
        return f"{confidence * 100:.0f}%"
    except (TypeError, ValueError):
        return "Not available"


def parse_food_items(value):
    if value is None:
        return []

    if isinstance(value, list):
        return value

    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []

    return []


def parse_response(response):
    if response is None:
        raise ValueError("Snowflake Cortex returned no response.")

    if isinstance(response, dict):
        result = response

    else:
        text = str(response).strip()

        text = re.sub(
            r"^```(?:json)?\s*|\s*```$",
            "",
            text,
            flags=re.IGNORECASE
        ).strip()

        text = (
            text.replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace('\\"', '"')
        )

        start = text.find("{")
        end = text.rfind("}")

        if start == -1 or end == -1:
            raise ValueError(
                f"AI did not return a JSON object: {text[:500]}"
            )

        text = text[start:end + 1]

        try:
            result = json.loads(text)

        except json.JSONDecodeError:
            try:
                result = ast.literal_eval(text)

            except (ValueError, SyntaxError) as error:
                raise ValueError(
                    f"AI response could not be parsed: {text[:800]}"
                ) from error

    if not isinstance(result, dict):
        raise ValueError(
            "AI response is not a valid nutrition assessment object."
        )

    food_items = result.get("food_items", [])

    if not isinstance(food_items, list):
        food_items = []

    cleaned_items = []

    for item in food_items:
        if not isinstance(item, dict):
            continue

        item.setdefault("food_name", "Unknown food")
        item.setdefault("estimated_quantity", "Not specified")
        item.setdefault("estimated_quantity_grams", 0)
        item.setdefault("calories", 0)
        item.setdefault("protein_grams", 0)
        item.setdefault("carbohydrates_grams", 0)
        item.setdefault("fat_grams", 0)
        item.setdefault("fiber_grams", 0)
        item.setdefault("confidence", 0)

        cleaned_items.append(item)

    result["food_items"] = cleaned_items
    result["food_item_count"] = len(cleaned_items)

    result.setdefault("cuisine_type", "unknown")
    result.setdefault("image_quality", "unknown")
    result.setdefault("total_calories", 0)
    result.setdefault("total_protein_grams", 0)
    result.setdefault("total_carbohydrates_grams", 0)
    result.setdefault("total_fat_grams", 0)
    result.setdefault("total_fiber_grams", 0)
    result.setdefault("overall_assessment", "")

    return result


# =========================================================
# IMAGE UPLOAD AND AI ANALYSIS
# =========================================================

def upload_image(file):
    image = Image.open(
        io.BytesIO(file.getvalue())
    ).convert("RGB")

    image.thumbnail((3000, 3000))

    image_stream = io.BytesIO()

    image.save(
        image_stream,
        format="JPEG",
        quality=90,
        optimize=True
    )

    image_stream.seek(0)

    original_name = file.name.rsplit(".", 1)[0]

    safe_name = re.sub(
        r"[^A-Za-z0-9._-]",
        "_",
        original_name
    )

    relative_path = (
        f"daily_food_logs/"
        f"{uuid.uuid4().hex}_{safe_name}.jpg"
    )

    upload_result = session.file.put_stream(
        image_stream,
        f"{STAGE}/{relative_path}",
        auto_compress=False,
        overwrite=True
    )

    if upload_result.status not in [
        "UPLOADED",
        "OVERWRITTEN"
    ]:
        raise ValueError(
            f"Image upload failed: {upload_result.status}"
        )

    return relative_path


def analyze_image(relative_path, meal_type, user_note):
    note_context = user_note.strip() if user_note else "No additional information provided."

    final_prompt = f"""
{PROMPT}

Additional user-provided meal context:
- Meal type: {meal_type}
- User note: {note_context}

Use the user-provided context when calculating the nutrition values.

Important rules:
- Treat the user note as additional meal information that may not be visible in the image.
- Include explicitly mentioned ingredients, quantities, scoops, servings, oils, sauces, supplements or drinks.
- Do not ignore a clearly stated quantity such as "25 g protein powder".
- Do not assume that "25 g scoop protein" means 25 g of protein. It means 25 g of protein powder unless the user explicitly says "25 g protein".
- Estimate nutrition for the stated ingredient using a reasonable standard nutritional value.
- Avoid counting an ingredient twice if it is also visible in the image.
- Mention in the overall assessment when nutrition includes user-provided information.
"""

    query = f"""
        SELECT AI_COMPLETE(
            '{MODEL}',
            ?,
            TO_FILE('{STAGE}', ?)
        ) AS RESPONSE
    """

    rows = session.sql(
        query,
        params=[final_prompt, relative_path]
    ).collect()

    if not rows:
        raise ValueError("Snowflake Cortex returned no rows.")

    return parse_response(rows[0]["RESPONSE"])

# =========================================================
# DATABASE FUNCTIONS
# =========================================================

def save_log(
    file,
    relative_path,
    analysis,
    meal_type,
    user_note
):
    query = f"""
        INSERT INTO {TABLE} (
            USER_NAME,
            LOG_DATE,
            LOG_TIME,
            MEAL_TYPE,
            USER_NOTE,
            FILE_NAME,
            STAGE_RELATIVE_PATH,
            CUISINE_TYPE,
            IMAGE_QUALITY,
            FOOD_ITEM_COUNT,
            FOOD_ITEMS,
            TOTAL_CALORIES,
            TOTAL_PROTEIN_GRAMS,
            TOTAL_CARBOHYDRATES_GRAMS,
            TOTAL_FAT_GRAMS,
            TOTAL_FIBER_GRAMS,
            OVERALL_ASSESSMENT,
            COMPLETE_AI_RESPONSE,
            AI_MODEL
        )
        SELECT
            CURRENT_USER(),
            CURRENT_DATE(),
            CURRENT_TIMESTAMP(),
            ?, ?, ?, ?, ?, ?, ?,
            PARSE_JSON(?),
            ?, ?, ?, ?, ?, ?,
            PARSE_JSON(?),
            ?
    """

    session.sql(
        query,
        params=[
            meal_type,
            user_note,
            file.name,
            relative_path,
            analysis["cuisine_type"],
            analysis["image_quality"],
            analysis["food_item_count"],
            json.dumps(analysis["food_items"]),
            analysis["total_calories"],
            analysis["total_protein_grams"],
            analysis["total_carbohydrates_grams"],
            analysis["total_fat_grams"],
            analysis["total_fiber_grams"],
            analysis["overall_assessment"],
            json.dumps(analysis),
            MODEL
        ]
    ).collect()


def get_today_summary():
    query = f"""
        SELECT
            COUNT(*) AS MEALS_LOGGED,
            COALESCE(
                SUM(TOTAL_CALORIES),
                0
            ) AS TOTAL_CALORIES,
            COALESCE(
                SUM(TOTAL_PROTEIN_GRAMS),
                0
            ) AS TOTAL_PROTEIN,
            COALESCE(
                SUM(TOTAL_CARBOHYDRATES_GRAMS),
                0
            ) AS TOTAL_CARBS,
            COALESCE(
                SUM(TOTAL_FAT_GRAMS),
                0
            ) AS TOTAL_FAT,
            COALESCE(
                SUM(TOTAL_FIBER_GRAMS),
                0
            ) AS TOTAL_FIBER
        FROM {TABLE}
        WHERE LOG_DATE = CURRENT_DATE()
          AND USER_NAME = CURRENT_USER()
    """

    return session.sql(query).collect()[0]


def get_today_logs():
    query = f"""
        SELECT
            LOG_ID,
            LOG_TIME,
            TO_CHAR(
                LOG_TIME,
                'HH12:MI AM'
            ) AS DISPLAY_TIME,
            MEAL_TYPE,
            USER_NOTE,
            FILE_NAME,
            CUISINE_TYPE,
            IMAGE_QUALITY,
            FOOD_ITEM_COUNT,
            FOOD_ITEMS,
            TOTAL_CALORIES,
            TOTAL_PROTEIN_GRAMS,
            TOTAL_CARBOHYDRATES_GRAMS,
            TOTAL_FAT_GRAMS,
            TOTAL_FIBER_GRAMS,
            OVERALL_ASSESSMENT
        FROM {TABLE}
        WHERE LOG_DATE = CURRENT_DATE()
          AND USER_NAME = CURRENT_USER()
        ORDER BY LOG_TIME DESC
    """

    return session.sql(query).collect()


# =========================================================
# TODAY'S LIVE SUMMARY
# =========================================================

st.subheader("Today's Nutrition")

summary = get_today_summary()

metric1, metric2, metric3 = st.columns(3)

metric1.metric(
    "Meals Logged",
    summary["MEALS_LOGGED"]
)

metric2.metric(
    "Calories So Far",
    f"{format_number(summary['TOTAL_CALORIES'])} kcal"
)

metric3.metric(
    "Protein So Far",
    f"{format_number(summary['TOTAL_PROTEIN'])} g"
)

metric4, metric5, metric6 = st.columns(3)

metric4.metric(
    "Carbohydrates",
    f"{format_number(summary['TOTAL_CARBS'])} g"
)

metric5.metric(
    "Fat",
    f"{format_number(summary['TOTAL_FAT'])} g"
)

metric6.metric(
    "Fiber",
    f"{format_number(summary['TOTAL_FIBER'])} g"
)

st.info(
    f"You have consumed approximately "
    f"**{format_number(summary['TOTAL_PROTEIN'])} g of protein** "
    f"and **{format_number(summary['TOTAL_CALORIES'])} calories** "
    f"so far today."
)


# =========================================================
# ADD A NEW MEAL
# =========================================================

st.divider()
st.subheader("Add a Meal")

input_col1, input_col2 = st.columns(2)

with input_col1:
    meal_type = st.selectbox(
        "Meal type",
        [
            "Breakfast",
            "Lunch",
            "Dinner",
            "Snack",
            "Drink"
        ],
        key="meal_type_input"
    )

with input_col2:
    user_note = st.text_input(
    "Additional meal details",
    placeholder="Example: Added one 25 g scoop of protein powder",
    help=(
        "Add ingredients or quantities that may not be visible in the image, "
        "such as protein powder, oil, sauces, sugar or milk."
    ),
    key="user_note_input"
)

uploaded_file = st.file_uploader(
    "Upload food plate image",
    type=[
        "jpg",
        "jpeg",
        "png",
        "webp"
    ],
    key=(
        f"food_uploader_"
        f"{st.session_state['uploader_key']}"
    )
)

if uploaded_file:
    preview_col, action_col = st.columns(
        [1.2, 1],
        gap="large"
    )

    with preview_col:
        st.image(
            uploaded_file,
            caption=uploaded_file.name,
            width=500
        )

    with action_col:
        st.write(
            "The visible food items and nutrition values "
            "will be analyzed and added to today's totals."
        )

        analyze_button = st.button(
            "Analyze and Add to Today",
            type="primary",
            width="stretch"
        )

        if analyze_button:
            try:
                with st.spinner(
                    "Analyzing food and estimating nutrition..."
                ):
                    relative_path = upload_image(
                        uploaded_file
                    )

                    analysis = analyze_image(
                            relative_path=relative_path,
                            meal_type=meal_type,
                            user_note=user_note
                    )

                    save_log(
                        file=uploaded_file,
                        relative_path=relative_path,
                        analysis=analysis,
                        meal_type=meal_type,
                        user_note=user_note
                    )

                # Preserve the latest result after resetting the form.
                st.session_state["latest_analysis"] = analysis
                st.session_state["latest_image"] = (
                    uploaded_file.getvalue()
                )
                st.session_state["latest_meal_type"] = meal_type
                st.session_state["latest_note"] = user_note

                # Reset uploader and meal form.
                st.session_state["uploader_key"] += 1
                st.session_state["reset_meal_form"] = True
                st.session_state["meal_saved"] = True

                st.rerun()

            except Exception as error:
                st.error(
                    f"Analysis failed: {error}"
                )


# =========================================================
# LATEST MEAL ANALYSIS
# =========================================================

if "latest_analysis" in st.session_state:
    analysis = st.session_state["latest_analysis"]

    st.divider()
    st.subheader("Latest Meal Analysis")

    image_col, analysis_col = st.columns(
        2,
        gap="large"
    )

    with image_col:
        st.image(
            st.session_state["latest_image"],
            caption=(
                st.session_state[
                    "latest_meal_type"
                ]
            ),
            width="stretch"
        )

        latest_note = st.session_state.get(
            "latest_note"
        )

        if latest_note:
            st.write(
                f"**Note:** {latest_note}"
            )

    with analysis_col:
        latest1, latest2, latest3 = st.columns(3)

        latest1.metric(
            "Food Items",
            analysis["food_item_count"]
        )

        latest2.metric(
            "Calories",
            (
                f"{format_number(analysis['total_calories'])} "
                f"kcal"
            )
        )

        latest3.metric(
            "Protein",
            (
                f"{format_number(analysis['total_protein_grams'])} "
                f"g"
            )
        )

        latest4, latest5, latest6 = st.columns(3)

        latest4.metric(
            "Carbs",
            (
                f"{format_number(analysis['total_carbohydrates_grams'])} "
                f"g"
            )
        )

        latest5.metric(
            "Fat",
            (
                f"{format_number(analysis['total_fat_grams'])} "
                f"g"
            )
        )

        latest6.metric(
            "Fiber",
            (
                f"{format_number(analysis['total_fiber_grams'])} "
                f"g"
            )
        )

        st.write(
            f"**Cuisine:** "
            f"{analysis['cuisine_type']}"
        )

        st.write(
            f"**Image quality:** "
            f"{analysis['image_quality']}"
        )

        st.write(
            f"**Assessment:** "
            f"{analysis['overall_assessment']}"
        )

        st.markdown(
            "### Identified Food Items"
        )

        if not analysis["food_items"]:
            st.warning(
                "No clearly visible food items were identified."
            )

        for index, item in enumerate(
            analysis["food_items"],
            start=1
        ):
            food_name = item.get(
                "food_name",
                "Unknown food"
            )

            with st.expander(
                f"{index}. {food_name}",
                expanded=True
            ):
                item1, item2, item3 = st.columns(3)

                item1.write(
                    f"**Quantity:** "
                    f"{item.get('estimated_quantity', 'Not specified')}"
                )

                item2.write(
                    f"**Weight:** "
                    f"{format_number(item.get('estimated_quantity_grams', 0))} g"
                )

                item3.write(
                    f"**Confidence:** "
                    f"{format_confidence(item.get('confidence'))}"
                )

                nutrient1, nutrient2, nutrient3 = st.columns(3)

                nutrient1.write(
                    f"**Calories:** "
                    f"{format_number(item.get('calories', 0))} kcal"
                )

                nutrient2.write(
                    f"**Protein:** "
                    f"{format_number(item.get('protein_grams', 0))} g"
                )

                nutrient3.write(
                    f"**Carbs:** "
                    f"{format_number(item.get('carbohydrates_grams', 0))} g"
                )

                nutrient4, nutrient5 = st.columns(2)

                nutrient4.write(
                    f"**Fat:** "
                    f"{format_number(item.get('fat_grams', 0))} g"
                )

                nutrient5.write(
                    f"**Fiber:** "
                    f"{format_number(item.get('fiber_grams', 0))} g"
                )

    with st.expander(
        "View complete JSON response"
    ):
        st.json(analysis)


# =========================================================
# TODAY'S COMPLETE LOG
# =========================================================

st.divider()
st.subheader("Today's Complete Food Log")

logs = get_today_logs()

if not logs:
    st.info(
        "No meals have been logged today."
    )

for log in logs:
    display_time = (
        log["DISPLAY_TIME"]
        or "Time unavailable"
    )

    title = (
        f"{log['MEAL_TYPE']} — "
        f"{display_time}"
    )

    with st.expander(title):
        st.write(
            f"**Note:** "
            f"{log['USER_NOTE'] or 'No note added'}"
        )

        st.write(
            f"**Cuisine:** "
            f"{log['CUISINE_TYPE'] or 'Unknown'}"
        )

        st.write(
            f"**Image quality:** "
            f"{log['IMAGE_QUALITY'] or 'Unknown'}"
        )

        st.write(
            f"**Food items identified:** "
            f"{log['FOOD_ITEM_COUNT']}"
        )

        log1, log2, log3 = st.columns(3)

        log1.metric(
            "Calories",
            (
                f"{format_number(log['TOTAL_CALORIES'])} "
                f"kcal"
            )
        )

        log2.metric(
            "Protein",
            (
                f"{format_number(log['TOTAL_PROTEIN_GRAMS'])} "
                f"g"
            )
        )

        log3.metric(
            "Carbohydrates",
            (
                f"{format_number(log['TOTAL_CARBOHYDRATES_GRAMS'])} "
                f"g"
            )
        )

        log4, log5 = st.columns(2)

        log4.metric(
            "Fat",
            (
                f"{format_number(log['TOTAL_FAT_GRAMS'])} "
                f"g"
            )
        )

        log5.metric(
            "Fiber",
            (
                f"{format_number(log['TOTAL_FIBER_GRAMS'])} "
                f"g"
            )
        )

        st.write(
            f"**Assessment:** "
            f"{log['OVERALL_ASSESSMENT'] or 'Not available'}"
        )

        food_items = parse_food_items(
            log["FOOD_ITEMS"]
        )

        if food_items:
            st.markdown(
                "#### Food Items"
            )

            for item in food_items:
                name = item.get(
                    "food_name",
                    "Unknown food"
                )

                st.write(
                    f"- **{name}:** "
                    f"{item.get('estimated_quantity', 'Not specified')} · "
                    f"{format_number(item.get('calories', 0))} kcal · "
                    f"{format_number(item.get('protein_grams', 0))} g protein"
                )


st.caption(
    "Food quantities and nutritional values are AI-generated estimates "
    "based on visible portions. They should not be treated as medically "
    "precise measurements."
)