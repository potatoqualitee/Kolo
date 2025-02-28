import os
import re
import yaml
import argparse
import requests
import hashlib
import logging
import random
import time
from pathlib import Path
from typing import Optional, List, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

# Initialize logging.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None
    logger.warning("OpenAI package not installed; openai provider will not work.")

# --- Helper Functions ---
def call_api(
    provider: str,
    model: str,
    prompt: str,
    global_ollama_url: Optional[str] = None,
    client: Optional[Any] = None
) -> Optional[str]:
    """
    Calls the appropriate API (OpenAI or Ollama) using the selected model and prompt.
    Implements an exponential backoff strategy for handling transient failures.
    """
    max_retries = 5
    backoff_factor = 1  # Base backoff time in seconds

    if provider.lower() == "openai":
        if client is None:
            logger.error("OpenAI client is not initialized.")
            return None

        attempt = 0
        while attempt <= max_retries:
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                )
                return response.choices[0].message.content.strip()
            except Exception as e:
                logger.error(f"OpenAI API error on attempt {attempt+1}/{max_retries}: {e}")
                if attempt == max_retries:
                    return None
                sleep_time = backoff_factor * (2 ** attempt) + random.uniform(0, 1)
                logger.info(f"Retrying OpenAI API call in {sleep_time:.2f} seconds...")
                time.sleep(sleep_time)
                attempt += 1

    elif provider.lower() == "ollama":
        if not global_ollama_url:
            logger.error("Global Ollama URL is not provided.")
            return None

        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {}
        }
        attempt = 0
        while attempt <= max_retries:
            try:
                response = requests.post(global_ollama_url, json=payload)
                response.raise_for_status()
                result = response.json()
                return result.get("response", "").strip()
            except Exception as e:
                logger.error(f"Ollama API error on attempt {attempt+1}/{max_retries}: {e}")
                if attempt == max_retries:
                    return None
                sleep_time = backoff_factor * (2 ** attempt) + random.uniform(0, 1)
                logger.info(f"Retrying Ollama API call in {sleep_time:.2f} seconds...")
                time.sleep(sleep_time)
                attempt += 1

    else:
        logger.error("Unknown provider specified.")
        return None

def find_file_in_subdirectories(base_dir: Path, relative_path: str) -> Optional[Path]:
    """
    Attempts to locate a file by its relative path in base_dir or its subdirectories.
    """
    possible_path = base_dir / relative_path
    if possible_path.exists():
        return possible_path

    target = Path(relative_path).name
    for path in base_dir.rglob(target):
        if path.is_file():
            return path
    return None

def parse_questions(question_text: str) -> List[str]:
    questions = []
    for line in question_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        # Remove leading numbering or bullets (like "1.", "-", "+", or "*")
        cleaned = re.sub(r'^[\d\.\-\+\*]+\s*', '', stripped)
        # Remove extra asterisks used for formatting
        cleaned = re.sub(r'\*+', '', cleaned).strip()
        # Check if this looks like a question
        if '?' in cleaned:
            questions.append(cleaned)
    return questions

def get_hash(text: str) -> str:
    """Returns the SHA-256 hash of the given text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def write_text_to_file(file_path: Path, text: str) -> None:
    """Writes text to a file ensuring the parent directory exists."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(text, encoding="utf-8")

def read_text_from_file(file_path: Path) -> str:
    """Reads and returns text from a file."""
    return file_path.read_text(encoding="utf-8")

def build_files_prompt(file_list: List[str], base_dir: Path, template: str) -> str:
    """
    Build a combined prompt string by iterating over a list of files
    using the provided individual prompt template.
    """
    combined = ""
    for rel_path in file_list:
        file_path = find_file_in_subdirectories(base_dir, rel_path)
        if file_path and file_path.exists():
            content = file_path.read_text(encoding="utf-8")
            combined += f"{template.format(file_name=rel_path)}\n\n{content}\n\n"
        else:
            logger.warning(f"{rel_path} not found in {base_dir} or its subdirectories.")
    return combined

# --- Main Processing Function ---
def process_file_group(
    group_name: str,
    group_config: Dict[str, Any],
    full_base_dir: Path,
    base_output_path: Path,
    question_provider_config: Dict[str, Any],
    answer_provider_config: Dict[str, Any],
    global_ollama_url: Optional[str],
    openai_client: Optional[Any],
    global_thread_count: int,
    question_personas: List[str] = None
) -> None:
    """
    Processes a group of files to generate questions and answers using LLM providers.
    Instead of submitting inner tasks to a global executor (which can deadlock with only 1 thread),
    we use local (nested) executors—or run sequentially if global_thread_count == 1.
    """
    file_list: List[str] = group_config.get("files", [])
    group_prompts = group_config.get("prompts", {})

    # Extract file group specific prompts.
    q_prompt_headers: List[str] = group_prompts.get("question_prompt_headers", [])
    q_prompt_footer: str = group_prompts.get("question_prompt_footer", "")
    q_file_prompt_header: str = group_prompts.get("question_file_prompt_header", "File: {file_name}")
    q_context_prompt: str = group_prompts.get("question_context_prompt", "{files_content}")
    a_file_prompt_header: str = group_prompts.get("answer_file_prompt_header", "File: {file_name}")
    a_context_prompt: str = group_prompts.get("answer_context_prompt", "{files_content}")
    a_question_prompt: str = group_prompts.get("answer_question_prompt", "Based on the file content provided, answer the following question in detail: {question}")

    try:
        iteration = int(group_name.split('_')[-1])
    except Exception as e:
        logger.warning(f"Could not determine iteration for group {group_name}: {e}")
        iteration = 1

    # Randomize file order for prompt construction.
    file_list_for_questions = file_list.copy()
    random.shuffle(file_list_for_questions)
    logger.info(f"[Group: {group_name}] File order for question generation: {file_list_for_questions}")
    combined_files_with_prompts = build_files_prompt(file_list_for_questions, full_base_dir, q_file_prompt_header)

    personas = question_personas if question_personas else [""]

    # Prepare directories.
    output_dir = base_output_path / "qa_generation_output"
    questions_dir = output_dir / "questions"
    answers_dir = output_dir / "answers"
    debug_dir = output_dir / "debug"
    questions_dir.mkdir(parents=True, exist_ok=True)
    answers_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    # --- QUESTION GENERATION ---
    generated_questions = []

    def generate_question_combo(h_idx: int, p_idx: int, header: str, persona: str) -> Optional[tuple]:
        persona_instructions = (f"Please use the following persona when generating the questions: {persona}."
                                if persona else "")
        final_question_prompt = (
            f"{q_context_prompt.format(files_content=combined_files_with_prompts)}\n\n"
            f"{header}\n\n"
            f"{persona_instructions}\n\n"
            f"{q_prompt_footer}"
        )
        question_file_name = f"questions_{group_name}_h{h_idx+1}_p{p_idx+1}.txt"
        question_debug_name = f"debug_{group_name}_h{h_idx+1}_p{p_idx+1}_questions.txt"
        question_file_path = questions_dir / question_file_name
        question_debug_path = debug_dir / question_debug_name

        logger.info(f"[Group: {group_name}] Generating questions using header {h_idx+1} and persona {p_idx+1}.")

        if question_file_path.exists():
            question_list_text = read_text_from_file(question_file_path).strip()
            logger.info(f"[Group: {group_name}] Questions file {question_file_name} already exists. Using existing questions.")
        else:
            question_list_text = call_api(
                provider=question_provider_config["provider"],
                model=question_provider_config["model"],
                prompt=final_question_prompt,
                global_ollama_url=global_ollama_url,
                client=openai_client,
            )
            if not question_list_text:
                logger.error(f"[Group: {group_name}] Failed to generate questions for header {h_idx+1} persona {p_idx+1}.")
                return None
            write_text_to_file(question_file_path, question_list_text)
            write_text_to_file(question_debug_path, final_question_prompt)
            logger.info(f"[Group: {group_name}] Questions saved to {question_file_path}")
            logger.info(f"[Group: {group_name}] Debug info saved to {question_debug_path}")

        return (h_idx, p_idx, question_list_text)

    # Decide on inner concurrency: if global_thread_count==1, run sequentially.
    inner_workers = global_thread_count if global_thread_count > 1 else 1

    # Prepare question-generation tasks.
    question_tasks = []
    for h_idx, header in enumerate(q_prompt_headers or [""]):
        for p_idx, persona in enumerate(personas):
            # Use default args to capture the current values.
            question_tasks.append(lambda h_idx=h_idx, p_idx=p_idx, header=header, persona=persona: 
                                  generate_question_combo(h_idx, p_idx, header, persona))

    if inner_workers > 1:
        with ThreadPoolExecutor(max_workers=inner_workers) as inner_pool:
            futures = [inner_pool.submit(task) for task in question_tasks]
            for future in as_completed(futures):
                result = future.result()
                if result is not None:
                    generated_questions.append(result)
    else:
        for task in question_tasks:
            result = task()
            if result is not None:
                generated_questions.append(result)

    # --- ANSWER GENERATION ---
    answer_tasks = []
    total_questions_overall = 0

    for (h_idx, p_idx, question_list_text) in generated_questions:
        questions = parse_questions(question_list_text)
        if not questions:
            logger.error(f"[Group: {group_name}] No valid questions found in generated list for header {h_idx+1} and persona {p_idx+1}.")
            continue

        logger.info(f"[Group: {group_name}] Found {len(questions)} questions for header {h_idx+1} and persona {p_idx+1}.")
        total_questions_overall += len(questions)

        for q_idx, question in enumerate(questions, start=1):
            answer_tasks.append((h_idx, p_idx, q_idx, question))

    logger.info(f"[Group: {group_name}] Total of {total_questions_overall} questions found. Beginning answer generation...")

    def process_question(h_idx: int, p_idx: int, q_idx: int, question: str) -> None:
        logger.info(f"[Group: {group_name}] Processing question h{h_idx+1} p{p_idx+1} q{q_idx}: {question}")

        answer_filename = f"answer_{group_name}_h{h_idx+1}_p{p_idx+1}_{q_idx}.txt"
        answer_debug_filename = f"debug_{group_name}_answer_h{h_idx+1}_p{p_idx+1}_{q_idx}.txt"
        meta_filename = f"answer_{group_name}_h{h_idx+1}_p{p_idx+1}_{q_idx}.meta"

        answer_file_path = answers_dir / answer_filename
        answer_debug_path = debug_dir / answer_debug_filename
        meta_file_path = answers_dir / meta_filename

        current_hash = get_hash(question)
        regenerate = True

        if answer_file_path.exists():
            if meta_file_path.exists():
                stored_hash = read_text_from_file(meta_file_path).strip()
                if stored_hash == current_hash:
                    logger.info(f"[Group: {group_name}] Answer h{h_idx+1} p{p_idx+1} q{q_idx} is up-to-date. Skipping regeneration.")
                    regenerate = False
                else:
                    logger.info(f"[Group: {group_name}] Question h{h_idx+1} p{p_idx+1} q{q_idx} has changed. Regenerating answer.")
            else:
                write_text_to_file(meta_file_path, current_hash)
                logger.info(f"[Group: {group_name}] Meta file created for answer h{h_idx+1} p{p_idx+1} q{q_idx}. Skipping regeneration.")
                regenerate = False

        if not answer_file_path.exists():
            logger.info(f"[Group: {group_name}] Generating answer for question h{h_idx+1} p{p_idx+1} q{q_idx}.")

        if not regenerate:
            return

        file_list_for_answers = file_list.copy()
        random.shuffle(file_list_for_answers)
        logger.info(f"[Group: {group_name}] File order for answer generation (h{h_idx+1} p{p_idx+1} q{q_idx}): {file_list_for_answers}")
        combined_files_for_answers = build_files_prompt(file_list_for_answers, full_base_dir, a_file_prompt_header)

        final_answer_prompt = (
            f"{a_context_prompt.format(files_content=combined_files_for_answers)}\n\n"
            f"{a_question_prompt.format(question=question)}"
        )

        answer_text = call_api(
            provider=answer_provider_config["provider"],
            model=answer_provider_config["model"],
            prompt=final_answer_prompt,
            global_ollama_url=global_ollama_url,
            client=openai_client,
        )
        if not answer_text:
            logger.error(f"[Group: {group_name}] Failed to generate answer for question h{h_idx+1} p{p_idx+1} q{q_idx}.")
            return

        write_text_to_file(answer_file_path, answer_text)
        write_text_to_file(answer_debug_path, final_answer_prompt)
        write_text_to_file(meta_file_path, current_hash)
        logger.info(f"[Group: {group_name}] Saved answer for question h{h_idx+1} p{p_idx+1} q{q_idx} -> {answer_file_path}")

    # Wrap answer tasks into callables.
    def process_answer_task(task_tuple):
        h_idx, p_idx, q_idx, question = task_tuple
        process_question(h_idx, p_idx, q_idx, question)

    if inner_workers > 1:
        with ThreadPoolExecutor(max_workers=inner_workers) as inner_pool:
            futures = [inner_pool.submit(process_answer_task, task) for task in answer_tasks]
            for future in as_completed(futures):
                future.result()
    else:
        for task in answer_tasks:
            process_answer_task(task)

# --- Main Script ---
def main() -> None:
    parser = argparse.ArgumentParser(description="Generate QA data for LLM fine-tuning.")
    parser.add_argument("--config", default="generate_qa_config.yaml", help="Path to configuration YAML file")
    parser.add_argument("--threads", type=int, default=8, help="Max workers for processing all tasks")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        logger.error(f"Configuration file not found: {config_path}")
        return

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    # Global settings.
    global_config = config.get("global", {})
    base_dir = global_config.get("base_dir", "")
    output_dir = global_config.get("output_dir", "output")
    output_base_path = Path(global_config.get("output_base_path", "/var/kolo_data"))
    full_base_dir = output_base_path / base_dir
    global_ollama_url = global_config.get("ollama_url", "http://localhost:11434/api/generate")

    # Provider settings.
    question_provider_config = config.get("providers", {}).get("question", {})
    answer_provider_config = config.get("providers", {}).get("answer", {})

    # Load question personas.
    question_personas = config.get("personas", {}).get("question_personas", [])

    # File groups.
    file_groups_config = config.get("file_groups", {})

    # Initialize OpenAI client if needed.
    openai_client = None
    if (question_provider_config.get("provider", "").lower() == "openai" or 
        answer_provider_config.get("provider", "").lower() == "openai"):
        if OpenAI is None:
            logger.error("OpenAI client cannot be initialized because the package is missing.")
        else:
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                logger.error("OPENAI_API_KEY environment variable not set.")
            else:
                openai_client = OpenAI(api_key=api_key)

    # Expand file groups by iterations.
    allowed_file_groups = {}
    for group_name, group_config in file_groups_config.items():
        iterations = group_config.get("iterations", 1)
        for i in range(1, iterations + 1):
            key = f"{group_name}_{i}"
            allowed_file_groups[key] = group_config

    total_groups = len(allowed_file_groups)
    logger.info(f"Starting processing of {total_groups} file groups.")

    # Use a global executor for file groups.
    with ThreadPoolExecutor(max_workers=args.threads) as executor:
        futures = []
        for group_name, group_config in allowed_file_groups.items():
            future = executor.submit(
                process_file_group,
                group_name=group_name,
                group_config=group_config,
                full_base_dir=full_base_dir,
                base_output_path=output_base_path,
                question_provider_config=question_provider_config,
                answer_provider_config=answer_provider_config,
                global_ollama_url=global_ollama_url,
                openai_client=openai_client,
                global_thread_count=args.threads,
                question_personas=question_personas
            )
            futures.append(future)
        for future in as_completed(futures):
            future.result()

    logger.info("All file groups have been processed.")

if __name__ == "__main__":
    main()
