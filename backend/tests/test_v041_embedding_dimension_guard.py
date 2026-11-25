"""#293：嵌入返回值的维度与形状必须当场校验（2026-11-26 收口）。

为什么钉：ChromaDB 在首次写入时按当次向量锁维度，之后后端换了维度不会报，只会在
add/query 时抛难以定位的 InvalidArgumentError。启动探测（_open_collection）已经兜住了
「库与后端不一致」那一种，但运行期上游换模型 / 换中转站时回来的向量会静默变成另一个
维度，直到写入那一刻才炸——而那时代码已经走了很远。

为什么不传 dimensions：不是所有上游都认这个参数（中转站式上游常直接忽略，甚至因未知
字段 400），传了反而多一种失败形状；「维度是多少」真正的判据是实际回来多少。
"""
import pytest

from app.memory.memory_manager import EmbeddingDimConflict, MemoryManager


class _Resp:
    def __init__(self, vec):
        self.data = [type("D", (), {"embedding": vec})()]


class _Emb:
    def __init__(self, vec):
        self.vec = vec

    def create(self, **kwargs):
        return _Resp(self.vec)


class _Client:
    def __init__(self, vec):
        self.embeddings = _Emb(vec)


@pytest.fixture()
def manager():
    """不碰真客户端、不碰 chroma：只造一个足以跑 _embed 的对象。"""
    m = MemoryManager.__new__(MemoryManager)
    m._injected_embed = None
    m.use_local_embed = False
    m._dummy_embed = False
    m.embed_model = "probe-model"
    m.embed_dim = 1024
    return m


def test_a_normal_vector_passes_through(manager):
    manager.client = _Client([0.1] * 1024)
    assert len(manager._embed("t")) == 1024


def test_a_non_vector_answer_is_rejected(manager):
    manager.client = _Client("not-a-vector")
    with pytest.raises(RuntimeError):
        manager._embed("t")


def test_a_changed_dimension_is_a_loud_conflict(manager):
    manager.client = _Client([0.1] * 384)
    with pytest.raises(EmbeddingDimConflict):
        manager._embed("t")
