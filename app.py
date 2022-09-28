import json
import os
import re

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
        if limitation:
            text_len = len(text)
            max_len = 60
            if is_phoneme:
                max_len *= 3
            else:
                if len(hps.data.text_cleaners) > 0 and hps.data.text_cleaners[0] == "zh_ja_mixture_cleaners":
                    text_len = len(re.sub("(\[ZH\]|\[JA\])", "", text))
            if text_len > max_len:
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


def create_soft_vc_fn(model, hps, speaker_ids):
    def soft_vc_fn(target_speaker, input_audio1, input_audio2):
        input_audio = input_audio1
        if input_audio is None:
            input_audio = input_audio2
        if input_audio is None:
            return "You need to upload an audio", None
        sampling_rate, audio = input_audio
        duration = audio.shape[0] / sampling_rate
        if limitation and duration > 15:
            return "Error: Audio is too long", None
        target_speaker_id = speaker_ids[target_speaker]

        audio = (audio / np.iinfo(audio.dtype).max).astype(np.float32)
        if len(audio.shape) > 1:
            audio = librosa.to_mono(audio.transpose(1, 0))
        if sampling_rate != 16000:
            audio = librosa.resample(audio, orig_sr=sampling_rate, target_sr=16000)
        with torch.inference_mode():
            units = hubert.units(torch.FloatTensor(audio).unsqueeze(0).unsqueeze(0))
        with no_grad():
            unit_lengths = LongTensor([units.size(1)])
            sid = LongTensor([target_speaker_id])
            audio = model.infer(units, unit_lengths, sid=sid, noise_scale=.667,
                                noise_scale_w=0.8)[0][0, 0].data.cpu().float().numpy()
        del units, unit_lengths, sid
        return "Success", (hps.data.sampling_rate, audio)

    return soft_vc_fn


def create_to_phoneme_fn(hps):
    def to_phoneme_fn(text):
        return _clean_text(text, hps.data.text_cleaners) if text != "" else ""

    return to_phoneme_fn


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
    models_tts = []
    models_vc = []
    models_soft_vc = []
    with open("saved_model/info.json", "r", encoding="utf-8") as f:
        models_info = json.load(f)
    for i, info in models_info.items():
        name = info["title"]
        lang = info["lang"]
        example = info["example"]
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

        t = info["type"]
        if t == "vits":
            models_tts.append((name, cover_path, speakers, lang, example,
                               hps.symbols, create_tts_fn(model, hps, speaker_ids),
                               create_to_phoneme_fn(hps)))
            models_vc.append((name, cover_path, speakers, create_vc_fn(model, hps, speaker_ids)))
        elif t == "soft-vits-vc":
            models_soft_vc.append((name, cover_path, speakers, create_soft_vc_fn(model, hps, speaker_ids)))

    hubert = torch.hub.load("bshall/hubert:main", "hubert_soft")

    app = gr.Blocks(css=css)

    with app:
        gr.Markdown("# Moe TTS And Voice Conversion Using VITS Model\n\n"
                    "![visitor badge](https://visitor-badge.glitch.me/badge?page_id=skytnt.moegoe)\n\n"
                    "unofficial demo for \n\n"
                    "- [https://github.com/CjangCjengh/MoeGoe](https://github.com/CjangCjengh/MoeGoe)\n"
                    "- [https://github.com/Francis-Komizu/VITS](https://github.com/Francis-Komizu/VITS)\n"
                    "- [https://github.com/luoyily/MoeTTS](https://github.com/luoyily/MoeTTS)\n"
                    "- [https://github.com/Francis-Komizu/Sovits](https://github.com/Francis-Komizu/Sovits)"
                    )
        with gr.Tabs():
            with gr.TabItem("TTS"):
                with gr.Tabs():
                    for i, (name, cover_path, speakers, lang, example, symbols, tts_fn,
                            to_phoneme_fn) in enumerate(models_tts):
                        with gr.TabItem(f"model{i}"):
                            with gr.Column():
                                gr.Markdown(f"## {name}\n\n"
                                            f"![cover](file/{cover_path})\n\n"
                                            f"lang: {lang}")
                                tts_input1 = gr.TextArea(label="Text (60 words limitation)", value=example,
                                                         elem_id=f"tts-input{i}")
                                tts_input2 = gr.Dropdown(label="Speaker", choices=speakers,
                                                         type="index", value=speakers[0])
                                tts_input3 = gr.Slider(label="Speed", value=1, minimum=0.5, maximum=2, step=0.1)
                                with gr.Accordion(label="Advanced Options", open=False):
                                    phoneme_input = gr.Checkbox(value=False, label="Phoneme input")
                                    to_phoneme_btn = gr.Button("Covert text to phoneme")
                                    phoneme_list = gr.Dataset(label="Phoneme list", components=[tts_input1],
                                                              samples=[[x] for x in symbols])
                                    phoneme_list_json = gr.Json(value=symbols, visible=False)
                                tts_submit = gr.Button("Generate", variant="primary")
                                tts_output1 = gr.Textbox(label="Output Message")
                                tts_output2 = gr.Audio(label="Output Audio")
                                tts_submit.click(tts_fn, [tts_input1, tts_input2, tts_input3, phoneme_input],
                                                 [tts_output1, tts_output2])
                                to_phoneme_btn.click(to_phoneme_fn, [tts_input1], [tts_input1])
                                phoneme_list.click(None, [phoneme_list, phoneme_list_json], [],
                                                   _js=f"""
                                (i,phonemes) => {{
                                    let text_input = document.querySelector("body > gradio-app");
                                    if (text_input.shadowRoot != null)
                                        text_input = text_input.shadowRoot;
                                    text_input = text_input.querySelector("#tts-input{i}").querySelector("textarea");
                                    let startPos = text_input.selectionStart;
                                    let endPos = text_input.selectionEnd;
                                    let oldTxt = text_input.value;
                                    let result = oldTxt.substring(0, startPos) + phonemes[i] + oldTxt.substring(endPos);
                                    text_input.value = result;
                                    text_input.focus()
                                    text_input.selectionStart = startPos + phonemes[i].length;
                                    text_input.selectionEnd = startPos + phonemes[i].length;
                                    text_input.blur()
                                }}""")

            with gr.TabItem("Voice Conversion"):
                with gr.Tabs():
                    for i, (name, cover_path, speakers, vc_fn) in enumerate(models_vc):
                        with gr.TabItem(f"model{i}"):
                            gr.Markdown(f"## {name}\n\n"
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
            with gr.TabItem("Soft Voice Conversion"):
                with gr.Tabs():
                    for i, (name, cover_path, speakers, soft_vc_fn) in enumerate(models_soft_vc):
                        with gr.TabItem(f"model{i}"):
                            gr.Markdown(f"## {name}\n\n"
                                        f"![cover](file/{cover_path})")
                            vc_input1 = gr.Dropdown(label="Target Speaker", choices=speakers, type="index",
                                                    value=speakers[0])
                            source_tabs = gr.Tabs()
                            with source_tabs:
                                with gr.TabItem("microphone"):
                                    vc_input2 = gr.Audio(label="Input Audio (15s limitation)", source="microphone")
                                with gr.TabItem("upload"):
                                    vc_input3 = gr.Audio(label="Input Audio (15s limitation)", source="upload")
                            vc_submit = gr.Button("Convert", variant="primary")
                            vc_output1 = gr.Textbox(label="Output Message")
                            vc_output2 = gr.Audio(label="Output Audio")
                            # clear inputs
                            source_tabs.set_event_trigger("change", None, [], [vc_input2, vc_input3],
                                                          js="()=>[null,null]")
                            vc_submit.click(soft_vc_fn, [vc_input1, vc_input2, vc_input3],
                                            [vc_output1, vc_output2])
    app.queue(concurrency_count=3).launch(show_api=False)
