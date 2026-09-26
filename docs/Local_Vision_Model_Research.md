# Local Vision Model Research

*Research completed 2026-09-20. Evaluates local/edge vision models for
replacing or complementing the Claude session detector in WinstonTracker.*

**Goal:** Identify a specific Great Dane (Winston) in Ring camera frames
without requiring an Anthropic API key or Claude session — running entirely
on the Mac Mini (Apple Silicon), free and open source.

**Current system:** A scheduled Claude session views staged frames every
30 minutes and returns a structured verdict (see `Session_Detection.md`).
This works but adds up to 30 minutes of detection latency and consumes
Claude subscription usage. A local vision layer could either replace
the Claude session entirely or serve as a fast pre-filter that skips
the session for obvious cases (no animal, cat, raccoon, clearly Winston).

---

## Recommended architecture: two-model pipeline

After evaluating all options, the recommended approach is a **two-layer
pipeline** that separates vision (local, free) from reasoning (Jev, near-free):

```
Ring event → 4 frames
  │
  ├─ Layer 0 (Apple Vision / YOLO): "is there a dog in this frame?"
  │   → bounding box crop of the dog region
  │
  ├─ Layer 1 (DINOv2, local, free): embed the crop → cosine similarity
  │   vs Winston reference gallery embeddings
  │   → structured features:
  │     { dog_detected: true,
  │       winston_similarity: 0.91,
  │       bounding_box: [x,y,w,h],
  │       size_estimate: "large",
  │       frame_quality: "good" }
  │
  └─ Layer 2 (Jev, ~free): takes structured features + camera_id
      + time_since_last_sighting + temporal_likelihood + Ring metadata
      → typed decision:
        { is_winston: true,
          confidence: 0.97,
          should_notify: true,
          priority: "normal" }
```

**Why two models instead of one:**
- Vision models understand pixels; reasoning models understand context.
  DINOv2 can tell you "this dog looks 91% like Winston" but it doesn't
  know that Winston was last seen 2 minutes ago at the neighboring camera,
  which makes a 91% visual match much more likely to be him.
- Jev takes structured state and returns typed decisions with calibrated
  probabilities — exactly the signal fusion that `fuse_signals` does today.
- Total cost per decision: ~$0.0004 (Jev) + $0 (local vision) = ~$0.0004.
  At 24 events/hour, that's ~$0.01/hour or ~$7/month.
- Total latency: ~200ms (DINOv2) + ~200ms (Jev API) = ~400ms per event.
  vs. up to 30 minutes with the current Claude session.

### Early access signup for Jev

**→ https://console.typesafe.ai/**

Sign up for early access. Jev launched into early access on 15 September
2026. As of this writing there is $5/month free usage on the console
billing page, and the model is free on Vercel's AI Gateway until
25 September 2026.

---

## Layer 2: Jev (TypeSafe AI's System One Model)

**What it is:** A non-autoregressive model that makes fast, structured
decisions. Not an LLM — it doesn't generate text. You send typed
questions against a state and get typed answers with calibrated
probabilities.

**Why it fits WinstonTracker:** Our `fuse_signals` function already
combines `is_winston_confidence`, `visual_similarity`,
`size_appearance_compatible`, and `temporal_likelihood` into a single
`winston_probability`. Jev does exactly this kind of structured decision
natively — and better, because its probabilities are calibrated against
outcomes.

| attribute | value |
|---|---|
| Speed | 70–500ms end-to-end |
| Pricing | $0.042/MTok input; output tokens free |
| Cost per decision | ~$0.0004 |
| Hallucination | Cannot hallucinate (typed outputs, not text generation) |
| Image support | **Not yet** — text/JSON state only |
| SDK | Python: `pip install typesafe-ai` |
| Primitives | Choice (pick from options), Score (0–N rubric), Noul (true/false with probability) |

**How we'd use it for Winston:**

```python
from typesafe import TypeSafe

client = TypeSafe(api_key="...")

response = client.systemone(
    model="jev-latest",
    state={
        "dog_detected": True,
        "winston_similarity": 0.91,
        "size_estimate": "large_breed",
        "camera_id": "outdoor-2",
        "seconds_since_last_sighting": 120,
        "last_seen_zone": "side-deck",
        "temporal_likelihood": 0.9,
        "ring_classification": "motion",
        "frame_quality": "good"
    },
    questions=[
        {
            "type": "noul",
            "name": "is_winston",
            "question": "Is this animal Winston, the specific Great Dane being tracked?"
        },
        {
            "type": "choice",
            "name": "priority",
            "question": "What notification priority should this sighting have?",
            "options": ["silent", "normal", "high"]
        }
    ]
)
```

**References:**
- Blog: https://typesafe.ai/blog/introducing-system-one-models-and-jev
- Docs: https://docs.typesafe.ai/
- Console / signup: https://console.typesafe.ai/
- Python SDK: https://github.com/typesafe-ai/system-one-adapter-python

---

## Layer 1: Local vision models (the perception layer)

### Category A: Image similarity / embeddings (RECOMMENDED)

These models embed images into vector space. Embed Winston's reference
photos once, then compare each new frame's embedding via cosine
similarity. This is the fastest and most accurate approach for "is this
the SAME dog?" rather than "is this A dog?"

#### DINOv2 — **Primary recommendation**

Meta's self-supervised vision model. Specifically designed for visual
feature extraction without labels.

| attribute | value |
|---|---|
| Parameters | ViT-S: 22M (~86MB), ViT-B: 86M (~330MB), ViT-L: 300M (~1.2GB) |
| Embedding dim | 384 (S), 768 (B), 1024 (L) |
| Apple Silicon | Yes — PyTorch MPS backend; convertible to Core ML via coremltools |
| Speed | ~40ms per image on GPU; ~100ms on Apple Silicon MPS (ViT-B) |
| Memory | ViT-B: ~1.5GB resident |
| Install | `pip install torch torchvision` — model loads from torch hub |
| Fine-grained accuracy | **5× better than CLIP** on iNaturalist species classification (70% vs 15% Top-1 on 10K species) |
| Instance matching | Better than CLIP at distinguishing two instances of the same type |
| License | Apache 2.0 |

**Why DINOv2 for Winston:** The key insight from the research is that
DINOv2 dramatically outperforms CLIP on fine-grained, same-species
instance matching. Telling apart "Winston" from "another Great Dane"
is exactly the iNaturalist-style problem where DINOv2 excels.

**Usage pattern:**
```python
import torch
from torchvision import transforms

model = torch.hub.load('facebookresearch/dinov2', 'dinov2_vitb14')
model.eval()

transform = transforms.Compose([
    transforms.Resize(256),
    transforms.CenterCrop(224),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406],
                         std=[0.229, 0.224, 0.225]),
])

# Embed reference images once at startup
reference_embeddings = []
for img in load_reference_images():
    with torch.no_grad():
        emb = model(transform(img).unsqueeze(0))
    reference_embeddings.append(emb)
gallery = torch.stack(reference_embeddings).mean(dim=0)  # centroid

# Per-frame: embed and compare
def winston_similarity(frame_crop):
    with torch.no_grad():
        emb = model(transform(frame_crop).unsqueeze(0))
    return torch.nn.functional.cosine_similarity(emb, gallery).item()
```

**Caveats:**
- Raw DINOv2 features are shape-aware, not identity-aware — frequent
  false positives on other Great Danes (or large dark dogs) are likely.
  The project's existing `visual_similarity` field in the verdict schema
  maps directly to this cosine score; the threshold needs calibration
  on logged frames.
- Specialized animal re-ID models (MegaDescriptor, MiewID) outperform
  DINOv2 by 20–70pp on re-ID benchmarks, but they require fine-tuning
  on a Winston gallery. Worth investigating if DINOv2 alone proves too
  noisy (see "Dog Re-ID Research" section below).
- For Core ML / Neural Engine acceleration: convert via coremltools
  (`ct.convert(traced_model, inputs=[ct.ImageType(...)]))`. ANE dispatch
  is automatic when the operator set allows it.

#### OpenCLIP — **Good alternative / complementary signal**

OpenAI's CLIP reimplemented with open training. Maps images and text
into a shared embedding space.

| attribute | value |
|---|---|
| Parameters | ViT-B/32: 151M, ViT-L/14: 428M |
| Apple Silicon | Yes — PyTorch MPS; MLX port (MobileCLIP) available |
| Speed | ViT-B/32: ~50–80ms per image on CPU after warmup |
| Memory | ViT-B/32: ~600MB resident |
| Install | `pip install open-clip-torch` |
| Zero-shot text queries | Yes — can ask "Great Dane" vs "person" vs "cat" via text |
| Instance matching | Weaker than DINOv2 for same-species individuals |
| License | MIT |

**Advantage over DINOv2:** CLIP can do zero-shot text classification
("is this a Great Dane?" "is this a person?") in addition to image
similarity. This makes it a good pre-filter: reject frames with
low "dog" similarity to the text prompt before running DINOv2.

**Complementary use:** Run CLIP text-similarity ("dog" vs "person" vs
"cat" vs "nothing") as a fast reject gate, then DINOv2 image-similarity
for the Winston-specific matching on frames that pass.

#### SigLIP — **Drop-in CLIP replacement**

Google's improvement on CLIP using sigmoid loss instead of softmax.
Better calibrated scores, scales to larger batch sizes, functionally
equivalent API. SigLIP 2 (Feb 2025) added multi-resolution training.
Available via Hugging Face Transformers. Use if CLIP's scores prove
poorly calibrated; otherwise stick with OpenCLIP for ecosystem maturity.

### Category B: Small vision-language models (VLMs)

These can answer free-form questions about images ("is this Winston?").
More flexible than embeddings but slower and heavier. Overkill if
embeddings + Jev cover the use case, but valuable as a fallback for
ambiguous frames.

#### Moondream — **Best small VLM for this use case**

Purpose-built for fast, focused visual QA. Smallest useful VLM.

| attribute | value |
|---|---|
| Parameters | Moondream 2: 1.86B; Moondream 3.1: 9B (2B active, MoE) |
| Apple Silicon | Yes — Photon engine has native Metal kernels |
| Speed (M4 Pro) | Moondream 2 4-bit: encode 95ms, decode 78 tok/s, **total 0.7s** |
| Memory | Moondream 2 4-bit: ~1.1GB; FP16: ~3.7GB |
| Install | `pip install moondream` (Photon); or via mlx-vlm |
| Can answer "is this Winston?" | Yes — can do visual QA with reference context |
| Few-shot reference | Moondream 3.1 can accept reference images in prompt |
| License | Apache 2.0 |

**Best for:** Fallback on ambiguous frames where embeddings are
inconclusive (similarity between 0.5–0.8). Ask Moondream "Is this a
large black Great Dane with a light collar and natural floppy ears?"
and get a yes/no with reasoning.

**Not ideal as the primary detector** because:
- 0.7s per frame × 4 frames = 2.8s per event (vs ~200ms for DINOv2)
- Cannot do reference-image matching as precisely as embeddings
- Still a text-generation model that can hallucinate

#### Qwen2.5-VL 3B — **Best general-purpose local VLM**

Alibaba's vision-language model. Best quality among small local VLMs.

| attribute | value |
|---|---|
| Parameters | 3B (also 7B variant) |
| Apple Silicon | Yes — strong MLX support via mlx-vlm |
| Speed (M5 Pro, 4-bit) | encode 130ms, decode 62 tok/s, **total 0.9s** |
| Memory | 3B 4-bit: ~2.2GB; 7B 4-bit: ~4.8GB |
| Install | `pip install mlx-vlm` |
| License | Apache 2.0 |

Higher capability than Moondream for complex reasoning but slower.
Would be the right choice if we wanted a single VLM to replace Claude
entirely rather than the two-model pipeline.

#### Other VLMs evaluated

| Model | Params | Speed on Apple Silicon | Verdict |
|---|---|---|---|
| Florence-2 | 0.23B / 0.77B | Several seconds on CPU; no native MPS optimization | Not recommended — CLIP/DINOv2 better for embeddings, Moondream better for QA |
| LLaVA 1.6 | 7B / 13B | 13B: 4.1s on M5 Pro; MLX support incomplete | Skip — Qwen2.5-VL is strictly better and faster |
| Phi-3-vision | 4.2B | Runs via MLX (phi-3-vision-mlx package) | Viable but less tested than Qwen on Apple Silicon |
| PaliGemma 2 | 3B | ~1s for constrained classification | Good for constrained vocabulary tasks; SigLIP encoder underneath |
| Gemma 4 E2B | 2.1B | ~38 tok/s on M2 Ultra at int4 | Multimodal, runs on Raspberry Pi. Worth testing but very new (April 2026) |

### Category C: Object detection (the "is there a dog?" layer)

#### YOLOv8 / YOLO11 / YOLO26 — **Best dog detector**

YOLO detects objects and returns bounding boxes with class labels.
The COCO-trained models include "dog" as a class (class 16).

| attribute | value |
|---|---|
| Models | YOLOv8n (nano, 3.2M params) through YOLO26 |
| Apple Silicon | PyTorch MPS; CoreML export for Neural Engine |
| Speed | CoreML export: **85 FPS** (YOLO11); YOLO26 CPU 43% faster than YOLO11 |
| Memory | YOLOv8n: ~6MB model; ~200MB runtime |
| Install | `pip install ultralytics` |
| Dog detection | Yes — COCO "dog" class out of the box |
| Breed classification | No — just "dog" vs "cat" vs "person" etc. |
| License | AGPL-3.0 (Ultralytics) |

**Role in pipeline:** First-stage filter. Run YOLO on each frame to:
1. Detect if a dog is present (skip frames with only people, cars, etc.)
2. Get the bounding box to crop the dog region
3. Estimate size from bbox-to-frame ratio (feeds `size_appearance_compatible`)

**Integration:**
```python
from ultralytics import YOLO

model = YOLO("yolo11n.pt")  # or export to CoreML for ANE speed
results = model(frame)

for box in results[0].boxes:
    if box.cls == 16:  # "dog" class
        crop = frame[int(box.xyxy[0][1]):int(box.xyxy[0][3]),
                     int(box.xyxy[0][0]):int(box.xyxy[0][2])]
        # → feed crop to DINOv2 for Winston similarity
```

---

## Apple-native options

### Apple Vision Framework — VNRecognizeAnimalsRequest

Built into macOS/iOS since iOS 13. Detects cats and dogs in images
and returns bounding boxes.

| attribute | value |
|---|---|
| Detects | Cats and dogs (revision 1) |
| Breed classification | No — cat/dog only, no breed |
| Speed | Near-instant (Neural Engine) |
| Memory | Negligible (system framework) |
| API | Swift: `VNRecognizeAnimalsRequest` / `RecognizeAnimalsRequest` |
| Python access | Via PyObjC bridge or Swift subprocess |
| Cost | Free, built-in |

**Role in pipeline:** Drop-in replacement for YOLO as the "is there
a dog?" pre-filter. Advantage: zero dependencies, runs on Neural Engine,
no model download. Disadvantage: no bounding box size estimation as
precise as YOLO, and calling from Python requires PyObjC or a Swift
helper binary.

**Swift helper approach:**
```swift
// swift_dog_detector.swift — compile once, call from Python
import Vision
import AppKit

let request = VNRecognizeAnimalsRequest()
let handler = VNImageRequestHandler(url: URL(fileURLWithPath: imagePath))
try handler.perform([request])

if let results = request.results {
    for observation in results {
        if observation.labels.contains(where: { $0.identifier == "Dog" }) {
            print("dog_detected,\(observation.boundingBox)")
        }
    }
}
```

### Apple Foundation Models (WWDC 2026) — Multimodal on-device

Apple's on-device LLM now accepts image input as of macOS 27 / iOS 27.

| attribute | value |
|---|---|
| Image input | Yes — UIImage, NSImage, CGImage, file URLs |
| On-device | Yes — AFM 3 Core Advanced (20B sparse, 1–4B active) |
| Speed | Not benchmarked publicly yet |
| API | Swift (FoundationModels framework); Python SDK available |
| Cost | Free (on-device inference) |
| Availability | macOS 27 beta (ships ~fall 2026) |
| Python SDK | `import apple_fm_sdk as fm` — unclear if image input works via Python yet |
| Hardware req | M-series chip; image input requires AFM 3 Core Advanced (high-end devices) |

**Assessment:** This is the most exciting long-term option — Apple's own
multimodal model running entirely on-device, for free, with no
dependencies. However:
- macOS 27 is still in beta (ships fall 2026)
- Image input requires the "Core Advanced" model variant — unclear if
  all Mac Minis qualify
- The Python SDK may not support image input yet (Swift API is primary)
- No benchmarks available for visual QA accuracy
- Cannot do reference-image comparison natively — would need to describe
  Winston in text and ask "does this match?"

**Verdict:** Monitor closely. Once macOS 27 ships and the Python SDK
confirms image support, this could replace Moondream as the "ambiguous
frame" fallback — completely free, no downloads, no dependencies.

### Create ML — Custom Winston classifier

Apple's ML training framework can train a binary image classifier
("Winston" vs "not Winston") from labeled photos.

| attribute | value |
|---|---|
| Training | Create ML app (GUI) or CreateML framework (Swift) |
| Input | Folders of labeled images (Winston/ and NotWinston/) |
| Output | .mlmodel file, runs on Neural Engine |
| Speed | Near-instant inference (Neural Engine optimized) |
| Accuracy | Depends on training data; typically 90%+ for binary classification |
| Training data | Winston reference photos + negative examples (other dogs, people, empty frames) |
| Cost | Free |

**Assessment:** The most direct approach for binary "is this Winston?"
but requires:
- Enough training images of Winston (ideally 50+, diverse conditions)
- Negative examples (other dogs, people, empty frames)
- Retraining when Winston's appearance changes (new collar, weight change)
- No similarity score — just binary classification with confidence

**Best used as:** An additional signal alongside DINOv2 embeddings.
Train once, export .mlmodel, run via `coremltools` in Python. The
Neural Engine inference is effectively free and instant.

### Core ML conversion of DINOv2/CLIP

Both DINOv2 and CLIP can be converted to Core ML format via `coremltools`
for Neural Engine acceleration:

```python
import coremltools as ct
import torch

model = torch.hub.load('facebookresearch/dinov2', 'dinov2_vits14')
model.eval()
traced = torch.jit.trace(model, torch.randn(1, 3, 224, 224))

mlmodel = ct.convert(
    traced,
    inputs=[ct.ImageType(name="image", shape=(1, 3, 224, 224))],
    compute_units=ct.ComputeUnit.ALL  # includes Neural Engine
)
mlmodel.save("dinov2_vits14.mlpackage")
```

**Benefit:** Potential 2–5× speedup over PyTorch MPS by leveraging the
Neural Engine's dedicated ML hardware. The ANE is specifically optimized
for transformer architectures at int8/int4 precision.

**Caveat:** ANE dispatch is automatic and not guaranteed — some operators
may fall back to GPU or CPU. Test actual performance with `computeUnits`
set to `.cpuAndNeuralEngine` vs `.all` to measure the real difference.

### VisionKit / Live Text

No animal recognition capabilities. Live Text is OCR-focused (text,
barcodes, QR codes). Not relevant for Winston detection.

---

## Dog re-identification research (for later)

If DINOv2 embeddings alone prove too noisy (confusing Winston with
other large dark dogs), these specialized models are the next step:

| Model | What it does | Stars | Status |
|---|---|---|---|
| MegaDescriptor | Wildlife re-ID embeddings; 20–70pp better than DINOv2 on re-ID benchmarks | — | Published, available on HuggingFace |
| MiewID | Animal individual ID; used by wildlife conservation | — | Published |
| AvitoTech/DINOv2-small-for-animal-identification | DINOv2 fine-tuned specifically for animal ID | — | On HuggingFace |
| WildlifeDatasets toolkit | Open-source animal re-ID evaluation framework | — | WACV 2024 paper |
| BIFOR | Background-invariant dog re-ID | <5★ | Academic |
| DogReID-1553 | Video-based dog re-ID dataset | <5★ | Academic |

**The recipe:** YOLO crop → specialized embedding model → cosine
similarity vs Winston gallery. This is the approach used by the
`ddyy-hash/dog-reid-…` project (YOLOv8 + SAM + OSNet).

---

## Existing projects: Ring + local ML

**No existing project combines Ring cameras with local ML for specific
pet identification.** The closest:

- **ha-llmvision** (1,474★): Home Assistant integration that sends camera
  frames to multimodal LLMs. Has reference image "Memory" feature.
  Closest prior art to our Claude session approach.
- **Frigate** (36,018★): NVR with real-time YOLO detection, zones, dog/cat
  labels. Needs continuous RTSP — incompatible with Ring. Design reference.
- **Ring's own pet detection** (Sep 2025): Ring cameras can now recognize
  pets and help locate lost dogs. But this is Ring's cloud service, not
  local, and no API for external use.
- Various YOLO → Telegram/Discord bots for pet detection (<5★ each).
  None do individual identification or multi-camera state.

---

## Implementation roadmap

### Phase 1: Local pre-filter (replaces ~80% of Claude sessions)

**Components:**
1. YOLO11n or Apple Vision `VNRecognizeAnimalsRequest` for dog detection
2. DINOv2 ViT-S (22M params) for Winston similarity
3. Threshold tuning on logged frames from `staging/archive/`

**Logic:**
```
similarity = dinov2_similarity(crop, winston_gallery)
if similarity > 0.85:    → auto-accept as Winston (skip Claude session)
if similarity < 0.30:    → auto-reject (not Winston, skip session)
if 0.30 ≤ sim ≤ 0.85:   → uncertain, queue for Claude session or Jev
```

**Estimated impact:**
- Most events are "no animal" or "person only" → auto-rejected
- Clear Winston sightings (good light, close range) → auto-accepted
- Only ambiguous cases (night/IR, partial view, other dogs) need the
  reasoning layer

**Dependencies:** `torch`, `torchvision`, `ultralytics` (or PyObjC for
Apple Vision). All run on Apple Silicon MPS. No API keys needed.

### Phase 2: Add Jev for structured decisions

**Replaces:** `fuse_signals` for the uncertain middle band.

**Input to Jev:** The same fields currently in `fuse_signals` —
`winston_similarity` (from DINOv2), `size_appearance_compatible` (from
YOLO bbox ratio), `temporal_likelihood` (from tracker), plus camera_id
and time context.

**Output from Jev:** Typed `is_winston` decision with calibrated
probability, directly consumable by the tracker as an Observation.

**Signup:** → **https://console.typesafe.ai/** (early access)

### Phase 3: Full replacement of Claude session (optional)

If Phases 1+2 prove accurate on the logged frame archive:
- Remove the scheduled Claude session for detection
- Keep `detector.mode: session` available as fallback
- Claude sessions shift to threshold tuning, anomaly review, and
  system monitoring rather than frame-by-frame detection

### Phase 4: Apple-native acceleration (when macOS 27 ships)

- Convert DINOv2 to Core ML for Neural Engine speed
- Evaluate Apple Foundation Models for the "ambiguous frame" tier
- Train a Create ML Winston classifier from accumulated frames
- Potentially run the entire pipeline on Neural Engine with zero
  external dependencies

---

## Speed comparison summary

| Approach | Per-frame latency | Per-event (4 frames) | Memory | Cost |
|---|---|---|---|---|
| Claude session (current) | N/A (batch) | up to 30 min | 0 (external) | Claude subscription |
| DINOv2 ViT-S embedding | ~100ms | ~400ms | ~500MB | Free |
| DINOv2 ViT-B embedding | ~100ms | ~400ms | ~1.5GB | Free |
| CLIP ViT-B/32 embedding | ~80ms | ~320ms | ~600MB | Free |
| YOLO11n detection | ~12ms (CoreML) | ~48ms | ~200MB | Free |
| Apple Vision animal detect | <10ms | <40ms | ~0 | Free |
| Moondream 2 (4-bit) VLM | ~700ms | ~2.8s | ~1.1GB | Free |
| Qwen2.5-VL 3B (4-bit) VLM | ~900ms | ~3.6s | ~2.2GB | Free |
| Jev API call | ~200ms | ~200ms (once) | 0 | $0.0004/decision |
| **Recommended pipeline** | | **~600ms total** | **~1.5GB** | **~$0.0004/event** |

---

## Decision matrix

| Requirement | DINOv2 + Jev | CLIP + Jev | Moondream alone | Qwen2.5-VL alone | Claude session (current) |
|---|---|---|---|---|---|
| Runs on Mac Mini (Apple Silicon) | ✅ | ✅ | ✅ | ✅ | ✅ |
| Specific dog ID (not just "a dog") | ✅ (similarity) | ⚠️ (weaker) | ⚠️ (text QA) | ⚠️ (text QA) | ✅ (best) |
| Reference image matching | ✅ (embeddings) | ✅ (embeddings) | ⚠️ (in-context) | ⚠️ (in-context) | ✅ (in-context) |
| Speed (near-real-time) | ✅ (~600ms) | ✅ (~500ms) | ⚠️ (~3s) | ⚠️ (~4s) | ❌ (30 min) |
| Free / open source | ⚠️ (Jev ~free) | ⚠️ (Jev ~free) | ✅ | ✅ | ⚠️ (subscription) |
| No API key needed | ❌ (Jev) | ❌ (Jev) | ✅ | ✅ | ❌ (subscription) |
| Accuracy on ambiguous frames | ✅ (calibrated) | ⚠️ | ⚠️ | ✅ | ✅ (best) |

---

## Sources

### Jev / TypeSafe AI
- [Introducing System One Models & Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev)
- [TypeSafe Docs](https://docs.typesafe.ai/)
- [Jev Console / Early Access Signup](https://console.typesafe.ai/)
- [Python SDK](https://github.com/typesafe-ai/system-one-adapter-python)
- [Jev pricing and speed analysis](https://www.orcarouter.ai/blog/jev-typesafe-system-one-what-we-know)
- [DataCamp: Jev System One Model](https://www.datacamp.com/blog/system-one-models-jev)

### Vision models
- [DINOv2 paper](https://arxiv.org/abs/2304.07193) — Meta, 13,349★
- [OpenCLIP](https://github.com/mlfoundations/open_clip) — 14,153★
- [Ultralytics YOLO](https://github.com/ultralytics/ultralytics) — 61,819★
- [Moondream](https://moondream.ai/) — [GitHub](https://github.com/m87-labs/moondream)
- [Photon 1.2.0 (Mac support)](https://moondream.ai/blog/photon-1-2-0-update)
- [SigLIP on HuggingFace](https://huggingface.co/docs/transformers/model_doc/siglip)

### Apple Silicon benchmarks
- [Local Vision LLMs on Apple Silicon: MLX vs llama.cpp (2026)](https://contracollective.com/blog/local-vision-llm-apple-silicon-mlx-qwen-vl-moondream-2026) — Definitive benchmark article
- [CLIP vs DINOv2 in image similarity](https://medium.com/aimonks/clip-vs-dinov2-in-image-similarity-6fa5aa7ed8c6)
- [DINOv2 fine-grained benchmarks (iNaturalist)](https://voxel51.com/blog/finding-the-best-embedding-model-for-image-classification)

### Apple native
- [VNRecognizeAnimalsRequest](https://developer.apple.com/documentation/vision/vnrecognizeanimalsrequest)
- [Creating an Image Classifier Model (Create ML)](https://developer.apple.com/documentation/createml/creating-an-image-classifier-model)
- [Apple Foundation Models WWDC 2026](https://byteiota.com/apple-foundation-models-wwdc-2026-multimodal-python-sdk/)
- [WWDC26 Machine Learning guide](https://developer.apple.com/wwdc26/guides/machine-learning/)
- [coremltools](https://github.com/apple/coremltools)

### Animal re-ID
- [AvitoTech/DINOv2-small-for-animal-identification](https://huggingface.co/AvitoTech/DINO-v2-small-for-animal-identification)
- [WildlifeDatasets toolkit](https://arxiv.org/abs/2311.09118)
- [Existing project survey](Dependencies_and_References.md) — §2, §3

### Ring + ML
- [ha-llmvision](https://github.com/valentinfrlch/ha-llmvision) — 1,474★, closest prior art
- [Frigate](https://github.com/blakeblackshear/frigate) — 36,018★, design reference
- [Ring pet detection announcement (Sep 2025)](https://techcrunch.com/2025/09/30/ring-cameras-can-now-recognize-faces-and-help-to-find-lost-pets/)


---

## Measured results, 2026-09-22 (P4-12 + P4-11 shipped)

Both layers were benchmarked against every archived event that already had a
recorded session verdict — 575 events, 215 of them Winston — rather than
against published benchmarks. Ground truth is the session verdict stored with
each observation, matched by `extra.staging_key`.

### Apple Vision gate (`src/dog_detector.py`)

| | gate PRESENT | gate ABSENT |
|---|---|---|
| session said "animal" (223) | 133 | **90** |
| session said "no animal" (352) | 0 | 352 |

8.7 ms/frame on the Neural Engine. PRESENT is perfect — zero false positives
in 352 animal-free events. ABSENT is not usable as a drop signal: it misses
40% of real animal events, 77 of them confident Winston sightings, 55 on
`side-deck` where he lies curled on his bed under a fisheye lens, often in
night IR. `VNRecognizeAnimalsRequest` finds standing and walking dogs.
`skip_on_absent` therefore defaults to **false**.

### DINOv2 ViT-S similarity (`src/local_detector.py`)

22.1M params, 384-dim, 8 ms/frame on MPS, ~20 ms/frame including I/O and the
crop. Gallery: the 6 enrolled reference photos, cached at `staging/gallery.pt`.

Score distribution (best frame per event, cropped to the gate box when there
is one):

| band | Winston | not Winston |
|---|---|---|
| 0.45–0.60 | 82 | 1 |
| 0.35–0.45 | 40 | 11 |
| 0.25–0.35 | 78 | 172 |
| < 0.25 | 15 | 176 |

* **Accept ≥ 0.50 with a gate box: 45 events, 45/45 correct.** That is 21% of
  all Winston events and 7.8% of all events.
* **Reject ≤ 0.10: 7 events, zero sightings lost.** Every higher cut-off
  costs real sightings (0.15 → 1, 0.20 → 10, 0.30 → 40), because a full-frame
  embedding scores the *scene*, and "the deck with Winston on it" and "the
  deck" are neighbours in embedding space.
* The single non-Winston event above 0.45 scored 0.494 and had a session
  confidence of 0.68 — just under the 0.70 ground-truth cut-off, so it is
  probably Winston too.

### What this does and does not buy

It removes the review round trip for ~8% of events at 100% measured
precision, and it gives every staged event a bounding box and a similarity
score as a hint for whoever reviews it. It does **not** make the backlog
disappear: 92% of events still need a verdict, and the volume is dominated by
person-only and empty-scene events that DINOv2 scores in the same range as
Winston lying on his bed. See ROADMAP P4-17 — a per-camera empty-scene
reference is the cheapest thing that targets that volume directly.
