"""Inference backends for exported models: ONNX Runtime and OpenVINO."""

from __future__ import annotations

import logging
from typing import Protocol

import numpy as np

from ..errors import DetectorError

log = logging.getLogger(__name__)

Shape = tuple[int | None, ...]


class InferenceBackend(Protocol):
    """Runs a model on one NCHW float tensor."""

    name: str
    device: str
    input_shape: Shape  # (N, C, H, W); None for dynamic dimensions

    def run(self, tensor: np.ndarray) -> list[np.ndarray]: ...

    def output_shapes(self) -> list[Shape]: ...


class OnnxRuntimeBackend:
    """ONNX Runtime. CPU by default; CUDA when onnxruntime-gpu is installed and requested."""

    name = "onnxruntime"

    def __init__(self, model_path: str, device: str = "auto", threads: int = 0) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise DetectorError(
                "onnxruntime is not installed. Run: pip install onnxruntime "
                "(or onnxruntime-gpu for NVIDIA GPUs)"
            ) from exc
        available = ort.get_available_providers()
        providers: list[str] = []
        wants_cuda = device.startswith("cuda") or device == "auto"
        if wants_cuda and "CUDAExecutionProvider" in available:
            providers.append("CUDAExecutionProvider")
        providers.append("CPUExecutionProvider")
        options = ort.SessionOptions()
        if threads:
            options.intra_op_num_threads = threads
        try:
            self._session = ort.InferenceSession(model_path, sess_options=options, providers=providers)
        except Exception as exc:  # noqa: BLE001
            raise DetectorError(f"Could not load ONNX model '{model_path}': {exc}") from exc
        first = self._session.get_inputs()[0]
        self._input_name = first.name
        self._dtype = np.float16 if "float16" in first.type else np.float32
        self.input_shape = tuple(d if isinstance(d, int) else None for d in first.shape)
        self._output_shapes = [
            tuple(d if isinstance(d, int) else None for d in o.shape)
            for o in self._session.get_outputs()
        ]
        active = self._session.get_providers()
        self.device = "cuda" if active and active[0] == "CUDAExecutionProvider" else "cpu"

    def run(self, tensor: np.ndarray) -> list[np.ndarray]:
        return self._session.run(None, {self._input_name: tensor.astype(self._dtype, copy=False)})

    def output_shapes(self) -> list[Shape]:
        return self._output_shapes


class OpenVinoBackend:
    """OpenVINO. Fast on Intel CPUs, and can use an Intel iGPU with device="GPU"."""

    name = "openvino"

    def __init__(self, model_path: str, device: str = "auto", threads: int = 0) -> None:
        try:
            import openvino as ov
        except ImportError as exc:
            raise DetectorError("openvino is not installed. Run: pip install openvino") from exc
        device_name = "CPU" if device in ("auto", "cpu") else device.upper()
        config: dict[str, object] = {"PERFORMANCE_HINT": "LATENCY"}
        if threads:
            config["INFERENCE_NUM_THREADS"] = threads
        try:
            core = ov.Core()
            model = core.read_model(model_path)
            self._compiled = core.compile_model(model, device_name, config)
        except Exception as exc:  # noqa: BLE001
            raise DetectorError(f"Could not load OpenVINO model '{model_path}': {exc}") from exc
        self.device = device_name.lower()
        self.input_shape = self._shape(self._compiled.input(0).partial_shape)
        self._output_shapes = [self._shape(o.partial_shape) for o in self._compiled.outputs]

    @staticmethod
    def _shape(partial) -> Shape:
        if partial.rank.is_dynamic:
            return ()
        return tuple(int(d.get_length()) if d.is_static else None for d in partial)

    def run(self, tensor: np.ndarray) -> list[np.ndarray]:
        result = self._compiled(tensor.astype(np.float32, copy=False))
        return [np.asarray(result[output]) for output in self._compiled.outputs]

    def output_shapes(self) -> list[Shape]:
        return self._output_shapes
