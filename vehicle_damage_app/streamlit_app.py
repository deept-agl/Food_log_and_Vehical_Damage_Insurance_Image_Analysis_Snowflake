import io
import json
import re
import uuid
import streamlit as st
import ast
from snowflake.snowpark.context import get_active_session

st.set_page_config(page_title="Vehicle Damage Assessment", page_icon="🚘", layout="wide")

session = get_active_session()

STAGE = "@VEHICLE_DAMAGE_AI_DB.DAMAGE_ANALYSIS.VEHICLE_IMAGES_STAGE"
TABLE = "VEHICLE_DAMAGE_AI_DB.DAMAGE_ANALYSIS.VEHICLE_DAMAGE_ASSESSMENTS"
MODEL = "openai-gpt-4.1"

st.title("🚘 Vehicle Insurance Damage Assessment")
st.caption("Upload a vehicle image to identify visible damage using Snowflake Cortex AI.")

PROMPT = """
You are assisting a motor insurance claims reviewer. Analyze the vehicle image and identify all clearly visible damage.

Return only valid JSON in this structure:
{
  "vehicle_view": "front, rear, left, right, front-left, front-right, rear-left, rear-right, interior or unknown",
  "image_quality": "good, acceptable, poor or unusable",
  "vehicle_description": "brief description of the visible vehicle",
  "damage_detected": true,
  "damage_count": 0,
  "overall_severity": "NONE, MINOR, MODERATE, MAJOR, CRITICAL or UNCERTAIN",
  "damages": [
    {
      "vehicle_part": "damaged part",
      "damage_type": "dent, scratch, crack, breakage, deformation, shattered glass, missing component, paint damage or other",
      "damage_location": "visible damage location",
      "severity": "MINOR, MODERATE, MAJOR, CRITICAL or UNCERTAIN",
      "description": "description of visible damage",
      "safety_concern": false,
      "confidence": 0.00
    }
  ],
  "overall_assessment": "summary of visible damage",
  "recommended_action": "recommended action for the claims reviewer"
}

Only report visible damage. Do not infer hidden or mechanical damage. Do not estimate repair cost. Do not treat reflections, dirt, shadows or normal panel gaps as damage. Return an empty damages array when no damage is visible. Return JSON only.
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
        raise ValueError("AI response is not a valid assessment object.")

    damages = result.get("damages", [])
    result["damages"] = damages if isinstance(damages, list) else []
    result["damage_count"] = len(result["damages"])
    result["damage_detected"] = result["damage_count"] > 0
    result.setdefault("vehicle_view", "unknown")
    result.setdefault("image_quality", "unknown")
    result.setdefault("vehicle_description", "")
    result.setdefault("overall_severity", "NONE" if not result["damages"] else "UNCERTAIN")
    result.setdefault("overall_assessment", "")
    result.setdefault("recommended_action", "Manual review recommended")

    return result


def upload_image(file):
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", file.name)
    relative_path = f"vehicle_claim_images/{uuid.uuid4().hex}_{safe_name}"
    session.file.put_stream(io.BytesIO(file.getvalue()), f"{STAGE}/{relative_path}", auto_compress=False, overwrite=True)
    return relative_path


def analyze_image(relative_path):
    query = f"""
        SELECT AI_COMPLETE(
            '{MODEL}',
            ?,
            TO_FILE('{STAGE}', ?)
        ) AS RESPONSE
    """
    response = session.sql(query, params=[PROMPT, relative_path]).collect()[0]["RESPONSE"]
    return parse_response(response)

def save_result(file, relative_path, analysis):
    query = f"""
        INSERT INTO {TABLE}
        (
            FILE_NAME, FILE_TYPE, FILE_SIZE_BYTES, STAGE_RELATIVE_PATH, STAGE_FULL_PATH,
            VEHICLE_VIEW, IMAGE_QUALITY, VEHICLE_DESCRIPTION, DAMAGE_DETECTED,
            DAMAGE_COUNT, OVERALL_SEVERITY, DAMAGES, OVERALL_ASSESSMENT,
            RECOMMENDED_ACTION, COMPLETE_AI_RESPONSE, AI_MODEL
        )
        SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, PARSE_JSON(?), ?, ?, PARSE_JSON(?), ?
    """

    session.sql(query, params=[
        file.name,
        file.type,
        file.size,
        relative_path,
        f"{STAGE}/{relative_path}",
        analysis["vehicle_view"],
        analysis["image_quality"],
        analysis["vehicle_description"],
        analysis["damage_detected"],
        analysis["damage_count"],
        analysis["overall_severity"],
        json.dumps(analysis["damages"]),
        analysis["overall_assessment"],
        analysis["recommended_action"],
        json.dumps(analysis),
        MODEL
    ]).collect()


uploaded_file = st.file_uploader("Upload vehicle image", type=["jpg", "jpeg", "png", "webp"])

if uploaded_file:
    st.image(uploaded_file, caption=uploaded_file.name, width=500)

    if st.button("Analyze Vehicle Damage", type="primary"):
        try:
            with st.spinner("Analyzing vehicle damage..."):
                path = upload_image(uploaded_file)
                analysis = analyze_image(path)
                save_result(uploaded_file, path, analysis)

            st.session_state["analysis"] = analysis
            st.session_state["image_bytes"] = uploaded_file.getvalue()
            st.success("Analysis completed and saved successfully.")

        except Exception as error:
            st.error(f"Analysis failed: {error}")

if "analysis" in st.session_state:
    analysis = st.session_state["analysis"]

    st.divider()
    image_col, analysis_col = st.columns(2, gap="large")

    with image_col:
        st.subheader("Uploaded Vehicle Image")
        st.image(st.session_state["image_bytes"], use_container_width=True)

    with analysis_col:
        st.subheader("AI Damage Analysis")

        metric1, metric2 = st.columns(2)
        metric1.metric("Damages Detected", analysis["damage_count"])
        metric2.metric("Overall Severity", analysis["overall_severity"])

        st.write(f"**Vehicle view:** {analysis['vehicle_view']}")
        st.write(f"**Image quality:** {analysis['image_quality']}")
        st.write(f"**Vehicle description:** {analysis['vehicle_description']}")
        st.write(f"**Overall assessment:** {analysis['overall_assessment']}")
        st.write(f"**Recommended action:** {analysis['recommended_action']}")

        st.markdown("### Damage Details")

        if not analysis["damages"]:
            st.success("No clearly visible damage was detected.")

        for index, damage in enumerate(analysis["damages"], start=1):
            with st.expander(f"Damage {index}: {damage.get('vehicle_part', 'Unknown part')}", expanded=True):
                st.write(f"**Damage type:** {damage.get('damage_type', 'Not specified')}")
                st.write(f"**Location:** {damage.get('damage_location', 'Not specified')}")
                st.write(f"**Severity:** {damage.get('severity', 'UNCERTAIN')}")
                st.write(f"**Description:** {damage.get('description', 'Not provided')}")
                st.write(f"**Safety concern:** {'Yes' if damage.get('safety_concern') else 'No'}")

                confidence = damage.get("confidence", 0)
                try:
                    st.write(f"**Confidence:** {float(confidence) * 100:.0f}%")
                except (TypeError, ValueError):
                    st.write("**Confidence:** Not available")

    with st.expander("View complete JSON response"):
        st.json(analysis)