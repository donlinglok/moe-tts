import json
import os
import librosa
import numpy as np
import torch
from torch import no_grad, LongTensor
import commons
import utils
import gradio as gr
from models import SynthesizerTrn
from text import text_to_sequence, _clean_text
from mel_processing import spectrogram_torch

limitation = os.getenv("SYSTEM") == "spaces"  # limit text and audio length in huggingface spaces


def get_text(text, hps, is_phoneme):
    text_norm = text_to_sequence(text, hps.symbols, [] if is_phoneme else hps.data.text_cleaners)
    if hps.data.add_blank:
        text_norm = commons.intersperse(text_norm, 0)
    text_norm = LongTensor(text_norm)
    return text_norm


def create_tts_fn(model, hps, speaker_ids):
    def tts_fn(text, speaker, speed, is_phoneme):
        if limitation and ((len(text) > 60 and not is_phoneme) or (len(text) > 120 and is_phoneme)):
            return "Error: Text is too long", None
        speaker_id = speaker_ids[speaker]
        stn_tst = get_text(text, hps, is_phoneme)
        with no_grad():
            x_tst = stn_tst.unsqueeze(0)
            x_tst_lengths = LongTensor([stn_tst.size(0)])
            sid = LongTensor([speaker_id])
            audio = model.infer(x_tst, x_tst_lengths, sid=sid, noise_scale=.667, noise_scale_w=0.8,
                                length_scale=1.0 / speed)[0][0, 0].data.cpu().float().numpy()
        del stn_tst, x_tst, x_tst_lengths, sid
        return "Success", (hps.data.sampling_rate, audio)

    return tts_fn


def create_vc_fn(model, hps, speaker_ids):
    def vc_fn(original_speaker, target_speaker, input_audio):
        if input_audio is None:
            return "You need to upload an audio", None
        sampling_rate, audio = input_audio
        duration = audio.shape[0] / sampling_rate
        if limitation and duration > 15:
            return "Error: Audio is too long", None
        original_speaker_id = speaker_ids[original_speaker]
        target_speaker_id = speaker_ids[target_speaker]

        audio = (audio / np.iinfo(audio.dtype).max).astype(np.float32)
        if len(audio.shape) > 1:
            audio = librosa.to_mono(audio.transpose(1, 0))
        if sampling_rate != hps.data.sampling_rate:
            audio = librosa.resample(audio, orig_sr=sampling_rate, target_sr=hps.data.sampling_rate)
        with no_grad():
            y = torch.FloatTensor(audio)
            y = y.unsqueeze(0)
            spec = spectrogram_torch(y, hps.data.filter_length,
                                     hps.data.sampling_rate, hps.data.hop_length, hps.data.win_length,
                                     center=False)
            spec_lengths = LongTensor([spec.size(-1)])
            sid_src = LongTensor([original_speaker_id])
            sid_tgt = LongTensor([target_speaker_id])
            audio = model.voice_conversion(spec, spec_lengths, sid_src=sid_src, sid_tgt=sid_tgt)[0][
                0, 0].data.cpu().float().numpy()
        del y, spec, spec_lengths, sid_src, sid_tgt
        return "Success", (hps.data.sampling_rate, audio)

    return vc_fn


css = """
        #advanced-btn {
            color: white;
            border-color: black;
            background: black;
            font-size: .7rem !important;
            line-height: 19px;
            margin-top: 24px;
            margin-bottom: 12px;
            padding: 2px 8px;
            border-radius: 14px !important;
        }
        #advanced-options {
            display: none;
            margin-bottom: 20px;
        }
"""

if __name__ == '__main__':
    models = []
    with open("saved_model/names.json", "r", encoding="utf-8") as f:
        models_names = json.load(f)
    for i, models_name in models_names.items():
        config_path = f"saved_model/{i}/config.json"
        model_path = f"saved_model/{i}/model.pth"
        cover_path = f"saved_model/{i}/cover.jpg"
        hps = utils.get_hparams_from_file(config_path)
        model = SynthesizerTrn(
            len(hps.symbols),
            hps.data.filter_length // 2 + 1,
            hps.train.segment_size // hps.data.hop_length,
            n_speakers=hps.data.n_speakers,
            **hps.model)
        utils.load_checkpoint(model_path, model, None)
        model.eval()
        speaker_ids = [sid for sid, name in enumerate(hps.speakers) if name != "None"]
        speakers = [name for sid, name in enumerate(hps.speakers) if name != "None"]

        models.append((models_name, cover_path, speakers, hps.symbols,
                       create_tts_fn(model, hps, speaker_ids), create_vc_fn(model, hps, speaker_ids)))

    app = gr.Blocks(css=css)

    with app:
        gr.Markdown("# Moe Japanese TTS And Voice Conversion Using VITS Model\n\n"
                    "![visitor badge](https://visitor-badge.glitch.me/badge?page_id=skytnt.moegoe)\n\n"
                    "unofficial demo for \n\n"
                    "- [https://github.com/CjangCjengh/MoeGoe](https://github.com/CjangCjengh/MoeGoe)\n"
                    "- [https://github.com/Francis-Komizu/VITS](https://github.com/Francis-Komizu/VITS)"
                    )
        with gr.Tabs():
            with gr.TabItem("TTS"):
                with gr.Tabs():
                    for i, (model_name, cover_path, speakers, symbols, tts_fn, vc_fn) in enumerate(models):
                        with gr.TabItem(f"model{i}"):
                            with gr.Column():
                                gr.Markdown(f"## {model_name}\n\n"
                                            f"![cover](file/{cover_path})")
                                tts_input1 = gr.TextArea(label="Text (60 words limitation)", value="こんにちは。")
                                tts_input2 = gr.Dropdown(label="Speaker", choices=speakers,
                                                         type="index", value=speakers[0])
                                tts_input3 = gr.Slider(label="Speed", value=1, minimum=0.5, maximum=2, step=0.1)
                                advanced_button = gr.Button("Advanced options", elem_id="advanced-btn")
                                advanced_options = gr.Column()
                                advanced_options.elem_id = "advanced-options"
                                with advanced_options:
                                    phoneme_input = gr.Checkbox(value=False, label="Phoneme input")
                                    to_phoneme_btn = gr.Button("Covert text to phoneme")
                                    phoneme_list = gr.Json(label="Phoneme list", value=symbols, elem_id="phoneme_list")

                                tts_submit = gr.Button("Generate", variant="primary")
                                tts_output1 = gr.Textbox(label="Output Message")
                                tts_output2 = gr.Audio(label="Output Audio")
                                advanced_button.click(None, [], [],
                                                      _js="""
                                                        () => {
                                                            const options = document.querySelector("body > gradio-app").querySelector("#advanced-options");
                                                            options.style.display = ["none", ""].includes(options.style.display) ? "flex" : "none";
                                                        }""")
                                tts_submit.click(tts_fn, [tts_input1, tts_input2, tts_input3, phoneme_input],
                                                 [tts_output1, tts_output2])
                                to_phoneme_btn.click(lambda x: _clean_text(x, hps.data.text_cleaners) if x != "" else x,
                                                     [tts_input1], [tts_input1])

            with gr.TabItem("Voice Conversion"):
                with gr.Tabs():
                    for i, (model_name, cover_path, speakers, symbols, tts_fn, vc_fn) in enumerate(models):
                        with gr.TabItem(f"model{i}"):
                            gr.Markdown(f"## {model_name}\n\n"
                                        f"![cover](file/{cover_path})")
                            vc_input1 = gr.Dropdown(label="Original Speaker", choices=speakers, type="index",
                                                    value=speakers[0])
                            vc_input2 = gr.Dropdown(label="Target Speaker", choices=speakers, type="index",
                                                    value=speakers[1])
                            vc_input3 = gr.Audio(label="Input Audio (15s limitation)")
                            vc_submit = gr.Button("Convert", variant="primary")
                            vc_output1 = gr.Textbox(label="Output Message")
                            vc_output2 = gr.Audio(label="Output Audio")
                            vc_submit.click(vc_fn, [vc_input1, vc_input2, vc_input3], [vc_output1, vc_output2])
    app.launch()
