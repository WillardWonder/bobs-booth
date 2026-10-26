# 1) Setup: GPU check + installs (OmniVoice, Flask, Cloudflare quick tunnel)
#    Before running: upload Bob's voice files with the Files panel (left), from the "Bob voice samples" folder on your Desktop:
#    bobnew_11m30-14m15.mp3 (his excited play-by-play) and bob_calm_1.wav to bob_calm_5.wav (his normal talking voice)
!nvidia-smi -L
!pip -q install omnivoice flask
!wget -q -O /usr/local/bin/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 && chmod +x /usr/local/bin/cloudflared
import torch, os; print("CUDA:", torch.cuda.is_available(), torch.__version__)
assert os.path.exists("/content/bobnew_11m30-14m15.mp3"), "Upload bobnew_11m30-14m15.mp3 and the bob_calm_*.wav files first (Files panel > upload)"
