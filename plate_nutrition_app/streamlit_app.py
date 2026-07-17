import io
import json
import re
import uuid
import ast
import streamlit as st
from PIL import Image
from snowflake.snowpark.context import get_active_session

st.set_page_config(page_title="Food Plate Nutrition Analysis", page_icon="🍽️", layout="wide")

session = get_active_session()

STAGE = "@IMAGE_ANALYSIS.PLATE_NUTRITION.FOODPLATE_IMAGES_STAGE"
TABLE = "IMAGE_ANALYSIS.PLATE_NUTRITION.PLATE_NUTRITION_ASSESSMENTS"
MODEL = "openai-gpt-4.1"

st.title("🍽️ Food Plate Nutrition Analysis")
st.caption("Upload a food plate image to identify visible food items and estimate nutritional details.")

PROMPT = """
Analyze food plate image and identify all clearly visible food items.

Return only valid JSON in this structure:
{
  "meal_type": "breakfast, lunch, dinner, snack or unknown",
  "cuisine_type": "cuisine type or unknown",
  "image_quality": "good, acceptable, poor or unusable",
  "food_items": [
    {
      "food_name": "identified food item",
      "estimated_quantity": "visible serving estimate",
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
  "overall_assessment": "short nutritional summary"
}

Identify only visible food items. Combine repeated portions of the same food. Do not invent hidden ingredients. Confidence must be between 0 and 1. Return JSON only.
"""


def parse_response(response):
    if isinstance(response, dict):
        result = response
    else:
        text = str(response).strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()

        # Convert literal \n, \t and escaped quotes into normal JSON text
        text = text.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')

        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            raise ValueError(f"AI did not return a JSON object: {text[:500]}")

        text = text[start:end + 1]

        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            try:
                result = ast.literal_eval(text)
            except (ValueError, SyntaxError) as error:
                raise ValueError(f"AI response could not be parsed: {text[:800]}") from error

    if not isinstance(result, dict):
        raise ValueError("AI response is not a valid nutrition assessment object.")

    food_items = result.get("food_items", [])
    result["food_items"] = food_items if isinstance(food_items, list) else []
    result["food_item_count"] = len(result["food_items"])

    result.setdefault("meal_type", "unknown")
    result.setdefault("cuisine_type", "unknown")
    result.setdefault("image_quality", "unknown")
    result.setdefault("total_calories", 0)
    result.setdefault("total_protein_grams", 0)
    result.setdefault("total_carbohydrates_grams", 0)
    result.setdefault("total_fat_grams", 0)
    result.setdefault("total_fiber_grams", 0)
    result.setdefault("overall_assessment", "")

    return result


def upload_image(file):
    image = Image.open(file)
    image = image.convert("RGB")

    # Reduce very large image dimensions while preserving aspect ratio
    image.thumbnail((3000, 3000))

    image_bytes = io.BytesIO()
    image.save(image_bytes, format="JPEG", quality=90)
    image_bytes.seek(0)

    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", file.name.rsplit(".", 1)[0])
    relative_path = f"food_plate_images/{uuid.uuid4().hex}_{safe_name}.jpg"
    full_path = f"{STAGE}/{relative_path}"

    upload_result = session.file.put_stream(
        image_bytes,
        full_path,
        auto_compress=False,
        overwrite=True
    )

    if upload_result.status not in ["UPLOADED", "OVERWRITTEN"]:
        raise ValueError(f"Image upload failed: {upload_result.status}")

    return relative_path

def analyze_image(relative_path):
    query = f"""
        SELECT AI_COMPLETE(
            '{MODEL}',
            ?,
            TO_FILE('{STAGE}', ?),
            OBJECT_CONSTRUCT('temperature', 0, 'max_tokens', 6000)
        ) AS RESPONSE
    """

    rows = session.sql(query, params=[PROMPT, relative_path]).collect()

    if not rows:
        raise ValueError("Snowflake Cortex returned no rows.")

    response = rows[0]["RESPONSE"]

    if response is None:
        raise ValueError(
            "Cortex could not process the image. The uploaded image was converted "
            "to JPEG, so verify the model works in this account using the SQL test."
        )

    return parse_response(response)

def save_result(file, relative_path, analysis):
    query = f"""
        INSERT INTO {TABLE}
        (
            FILE_NAME, FILE_TYPE, FILE_SIZE_BYTES, STAGE_RELATIVE_PATH,
            MEAL_TYPE, CUISINE_TYPE, IMAGE_QUALITY, FOOD_ITEM_COUNT,
            FOOD_ITEMS, TOTAL_CALORIES, TOTAL_PROTEIN_GRAMS,
            TOTAL_CARBOHYDRATES_GRAMS, TOTAL_FAT_GRAMS,
            TOTAL_FIBER_GRAMS, OVERALL_ASSESSMENT,
            COMPLETE_AI_RESPONSE, AI_MODEL
        )
        SELECT ?, ?, ?, ?, ?, ?, ?, ?, PARSE_JSON(?), ?, ?, ?, ?, ?, ?, PARSE_JSON(?), ?
    """

    session.sql(query, params=[
        file.name,
        file.type,
        file.size,
        relative_path,
        analysis["meal_type"],
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
    ]).collect()


uploaded_file = st.file_uploader("Upload food plate image", type=["jpg", "jpeg", "png", "webp"])

if uploaded_file:
    st.image(uploaded_file, caption=uploaded_file.name, width=500)

    if st.button("Analyze Food Plate", type="primary"):
        try:
            with st.spinner("Analyzing food plate..."):
                path = upload_image(uploaded_file)
                analysis = analyze_image(path)
                save_result(uploaded_file, path, analysis)

            st.session_state["nutrition_analysis"] = analysis
            st.session_state["food_image_bytes"] = uploaded_file.getvalue()
            st.success("Analysis completed and saved successfully.")

        except Exception as error:
            st.error(f"Analysis failed: {error}")


if "nutrition_analysis" in st.session_state:
    analysis = st.session_state["nutrition_analysis"]

    st.divider()
    image_col, analysis_col = st.columns(2, gap="large")

    with image_col:
        st.subheader("Uploaded Food Plate")
        st.image(st.session_state["food_image_bytes"], width="stretch")

    with analysis_col:
        st.subheader("AI Nutrition Analysis")

        metric1, metric2, metric3 = st.columns(3)
        metric1.metric("Food Items", analysis["food_item_count"])
        metric2.metric("Calories", f"{analysis['total_calories']} kcal")
        metric3.metric("Protein", f"{analysis['total_protein_grams']} g")

        metric4, metric5, metric6 = st.columns(3)
        metric4.metric("Carbohydrates", f"{analysis['total_carbohydrates_grams']} g")
        metric5.metric("Fat", f"{analysis['total_fat_grams']} g")
        metric6.metric("Fiber", f"{analysis['total_fiber_grams']} g")

        st.write(f"**Meal type:** {analysis['meal_type']}")
        st.write(f"**Cuisine type:** {analysis['cuisine_type']}")
        st.write(f"**Image quality:** {analysis['image_quality']}")
        st.write(f"**Overall assessment:** {analysis['overall_assessment']}")

        st.markdown("### Food Item Details")

        if not analysis["food_items"]:
            st.warning("No clearly visible food items were identified.")

        for index, item in enumerate(analysis["food_items"], start=1):
            with st.expander(f"Item {index}: {item.get('food_name', 'Unknown food')}", expanded=True):
                st.write(f"**Estimated quantity:** {item.get('estimated_quantity', 'Not specified')}")
                st.write(f"**Estimated weight:** {item.get('estimated_quantity_grams', 0)} g")
                st.write(f"**Calories:** {item.get('calories', 0)} kcal")
                st.write(f"**Protein:** {item.get('protein_grams', 0)} g")
                st.write(f"**Carbohydrates:** {item.get('carbohydrates_grams', 0)} g")
                st.write(f"**Fat:** {item.get('fat_grams', 0)} g")
                st.write(f"**Fiber:** {item.get('fiber_grams', 0)} g")

                confidence = item.get("confidence", 0)
                try:
                    st.write(f"**Confidence:** {float(confidence) * 100:.0f}%")
                except (TypeError, ValueError):
                    st.write("**Confidence:** Not available")

    with st.expander("View complete JSON response"):
        st.json(analysis)

    st.caption("Portion sizes and nutritional values are AI-generated estimates based only on the visible image.")