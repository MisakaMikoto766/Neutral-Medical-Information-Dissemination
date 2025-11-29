import json
import random
import requests
import time
from tqdm import tqdm
import os
import re


CONFIG_FILE = "configfile"
with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config = json.load(f)

API_KEY = config["api_key"]
BASE_URL = config["api_url"]
MODEL = config.get("model", "modelname")
HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {API_KEY}",
}

SHARING_PROMPT = config["prompts"]["sharing"]
PRE_SURVEY_PROMPT = config["prompts"]["survey"]["before"]
# print(PRE_SURVEY_PROMPT)
POST_SURVEY_PROMPT = config["prompts"]["survey"]["after"]


USER_FILE = "jsonfile"
NEWS_FILE = "jsonfile"
OUTPUT_FILE = "file"


MAX_SAMPLE_MODERATE = 5
MAX_SAMPLE_WEAK = 3
TEST_LIMIT = 2  
MAX_DEPTH = 3  


def call_api(prompt, retries=3, delay=2):
    data = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "You are a virtual patient deciding whether and to whom to share a piece of health news."},
            {"role": "user", "content": prompt}
        ],
        "thinking": {"type": "disabled"},
    }
    for attempt in range(retries):
        try:
            response = requests.post(BASE_URL, headers=HEADERS, json=data, timeout=60)
            if response.status_code == 200:
                result = response.json()
                return result["choices"][0]["message"]["content"]
            else:
                print(f"Error {response.status_code}: {response.text}")
        except Exception as e:
            print(f"Exception on attempt {attempt+1}: {e}")
        time.sleep(delay)
    print("Failed after retries.")
    return "none"


with open(USER_FILE, "r", encoding="utf-8") as f:
    users = json.load(f)
with open(NEWS_FILE, "r", encoding="utf-8") as f:
    news_list = json.load(f)


def get_user(user_id: int):
    for u in users:
        if u["User Profile"]["id"] == user_id:
            return u
    return None

def normalize_disease_name(name: str):
    name = name.lower()  
    name = re.sub(r"\(.*?\)", "", name)
    name = re.sub(r"[^a-z\s]", " ", name)  
    name = re.sub(r"\s+", " ", name).strip()  
    return name

def disease_match(news_disease: str, patient_disease: str):
    n_norm = normalize_disease_name(news_disease)
    p_norm = normalize_disease_name(patient_disease)

    if n_norm in p_norm or p_norm in n_norm:
        return True
    n_tokens = set(n_norm.split())
    p_tokens = set(p_norm.split())
    overlap = n_tokens & p_tokens  
    common_ignore = {"acute", "chronic", "type", "syndrome", "disease"}
    overlap_meaningful = {t for t in overlap if t not in common_ignore}
    return len(overlap_meaningful) > 0

def find_users_with_disease(disease_name: str):
    matched = []
    for u in users:
        disease = u.get("Disease Information", {}).get("Disease", "")
        if not disease:
            continue
        if disease.lower() == disease_name.lower():  
            matched.append(u["User Profile"]["id"])
    return matched


def sample_relations(relations: dict):
    sampled = {"strong": [], "moderate": [], "weak": []}
    for rel_type, rel_list in relations.items():
        if rel_type == "strong":
            sampled[rel_type] = rel_list
        elif rel_type == "moderate":
            sampled[rel_type] = random.sample(rel_list, min(MAX_SAMPLE_MODERATE, len(rel_list)))
        elif rel_type == "weak":
            sampled[rel_type] = random.sample(rel_list, min(MAX_SAMPLE_WEAK, len(rel_list)))
    return sampled

def build_prompt(user_info, news_text, neighbor_infos, stage="sharing"):
    clean_user = {k: v for k, v in user_info.items() if k not in ["Examination Results", "Treatment Plan", "Relations"]}
    
    if stage == "sharing":
        prompt = SHARING_PROMPT.format(
            profile=json.dumps(clean_user, indent=2, ensure_ascii=False),
            news_text=news_text,
            neighbor_infos=json.dumps(neighbor_infos, indent=2, ensure_ascii=False)
        )
    elif stage == "pre_survey":
        prompt = PRE_SURVEY_PROMPT.format(
            profile=json.dumps(clean_user, indent=2, ensure_ascii=False),
            news_text=news_text
        )
    elif stage == "post_survey":
        prompt = POST_SURVEY_PROMPT.format(
            profile=json.dumps(clean_user, indent=2, ensure_ascii=False),
            news_text=news_text
        )
    return prompt


def icm_propagation(news_item, start_user_id, used_users):

    text = news_item["content"]
    disease = news_item["disease"]
    activated = set([start_user_id])
    all_feedback = []
    current_round = [start_user_id]
    depth = 0 

    while current_round and depth < MAX_DEPTH: 
        depth += 1
        print(f" 🔄 Round {depth} (active={len(current_round)})")
        next_round = []

        for uid in current_round:
            user_info = get_user(uid)
            if not user_info:
                continue
            relations = user_info["Relations"]
            sampled_relations = sample_relations(relations)
            neighbor_infos = {}
            for tie_type, neighbor_list in sampled_relations.items():
                neighbor_infos[tie_type] = []
                for nid in neighbor_list:
                    if nid in used_users or nid in activated:
                        continue
                    ninfo = get_user(nid)
                    if ninfo:
                        neighbor_infos[tie_type].append({
                            "id": nid,
                            "patient_info": ninfo.get("Patient Information", {}),
                            "profile": ninfo.get("User Profile", {}),
                            "disease": ninfo["Disease Information"].get("Disease", "")
                        })
            
            pre_survey_prompt = build_prompt(user_info, text, neighbor_infos, stage="pre_survey")
            pre_survey_response = call_api(pre_survey_prompt)

            sharing_prompt = build_prompt(user_info, text, neighbor_infos, stage="sharing")
            share_response = call_api(sharing_prompt)
            print(f"  Share decision for {uid} -> {share_response}")

            try:
                parts = share_response.split(";")
                emotion = float(parts[0].split(":")[1].strip())
                willingness = float(parts[1].split(":")[1].strip())
                credibility = float(parts[2].split(":")[1].strip())
                share_part = parts[3].split(":")[1].strip()
                share_ids = json.loads(share_part)
            except Exception as e:
                print(f"Parse error: {e}")
                emotion, willingness, credibility, share_ids = 0.5, 0.5, 0.5, []

            post_survey_prompt = build_prompt(user_info, text, neighbor_infos, stage="post_survey")
            post_survey_response = call_api(post_survey_prompt)
            # print(f"  Post-Survey for {uid} -> {post_survey_response}")

            feedback = {
                "user_id": uid,
                "Emotion": emotion,
                "Willingness": willingness,
                "Credibility": credibility,
                "Share_to": share_ids,
                "Pre_Survey_Response": pre_survey_response,  
                "Post_Survey_Response": post_survey_response  
            }
            all_feedback.append(feedback)

            for sid in share_ids:
                if sid not in activated and sid not in used_users:
                    next_round.append(sid)

        activated.update(next_round)
        if len(next_round) == 0:
            break
        current_round = next_round

    breadth = len(activated)
    total_users = len(users)
    rate = round(breadth / total_users, 4)

    return {
        "root": start_user_id,
        "activated_users": list(activated),
        "feedback": all_feedback,
        "depth": depth,
        "breadth": breadth,
        "rate": rate
    }


results = []
os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    f.write("[\n")  
    for idx, news in enumerate(tqdm(news_list, desc="Simulating propagation")):
        disease = news["disease"]
        start_users = find_users_with_disease(disease)
        print(f"\n🩺 News {news['id']} | Disease: {disease} | Start users: {len(start_users)}")
        used_users = set()
        chains = []
        all_depths, all_breadths = [], []

        for su in start_users:
            if su in used_users:
                continue
            print(f"Start propagation from user {su}")
            chain_result = icm_propagation(news, su, used_users)
            chains.append(chain_result)
            all_depths.append(chain_result["depth"])
            all_breadths.append(chain_result["breadth"])

        if all_depths:
            avg_depth = round(sum(all_depths) / len(all_depths), 2)
            avg_breadth = round(sum(all_breadths) / len(all_breadths), 2)
            avg_rate = round(sum(all_breadths) / len(all_breadths) / len(users), 4)
        else:
            avg_depth = avg_breadth = avg_rate = 0

        news_result = {
            "news_id": news["id"],
            "disease": disease,
            "chains": chains,
            "summary": {
                "avg_depth": avg_depth,
                "avg_breadth": avg_breadth,
                "avg_rate": avg_rate
            }
        }
        json.dump(news_result, f, indent=2, ensure_ascii=False)
        if idx < len(news_list) - 1:
            f.write(",\n")  
        f.flush()  
    f.write("\n]\n") 

print(f"\n Simulation completed! Partial results saved to: {OUTPUT_FILE}")
