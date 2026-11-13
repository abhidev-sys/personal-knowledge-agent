import hashlib
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.enums import (
    EvidenceType,
    MemoryAction,
    MemoryActor,
    MemoryStatus,
    MemoryType,
    MemoryVerification,
    MessageRole,
    NodeType,
    RelationOrigin,
    RelationStatus,
    RelationType,
    SourceType,
)
from app.db.models import (
    Conversation,
    DocumentChunk,
    KnowledgeNode,
    KnowledgeRelation,
    Memory,
    MemoryEvidence,
    MemoryHistory,
    Message,
    MessageCitation,
    MessageMemory,
    RelationEvidence,
    Source,
    User,
)
from app.db.types import utcnow


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def add(session: Session, obj):
    session.add(obj)
    session.flush()
    return obj


def make_user(s: Session) -> User:
    return add(s, User(email=f"{uuid.uuid4().hex[:10]}@example.com"))


def make_source(s: Session, owner: User) -> Source:
    return add(
        s,
        Source(
            owner_id=owner.id,
            source_type=SourceType.NOTE,
            title="A note",
            content_text="hello",
            content_hash=sha(uuid.uuid4().hex),
        ),
    )


def make_chunk(s: Session, source: Source, sequence: int = 0) -> DocumentChunk:
    return add(
        s,
        DocumentChunk(
            source_id=source.id,
            owner_id=source.owner_id,
            sequence=sequence,
            content="chunk text",
            content_hash=sha(f"c{sequence}{uuid.uuid4().hex}"),
        ),
    )


def make_memory(s: Session, owner: User, **overrides) -> Memory:
    fields = dict(
        owner_id=owner.id, memory_type=MemoryType.PREFERENCE, statement="I prefer concise answers."
    )
    fields.update(overrides)
    return add(s, Memory(**fields))


def make_conversation(s: Session, owner: User) -> Conversation:
    return add(s, Conversation(owner_id=owner.id, title="chat"))


def make_message(s: Session, conv: Conversation, owner_id=None) -> Message:
    return add(
        s,
        Message(
            conversation_id=conv.id,
            owner_id=owner_id or conv.owner_id,
            role=MessageRole.ASSISTANT,
            content="answer",
        ),
    )


def make_source_node(s: Session, source: Source) -> KnowledgeNode:
    return add(
        s, KnowledgeNode(owner_id=source.owner_id, node_type=NodeType.SOURCE, source_id=source.id)
    )


def make_concept(s: Session, owner: User, label: str) -> KnowledgeNode:
    return add(s, KnowledgeNode(owner_id=owner.id, node_type=NodeType.CONCEPT, label=label))


def make_relation(s: Session, a: KnowledgeNode, b: KnowledgeNode, **overrides) -> KnowledgeRelation:
    fields = dict(
        owner_id=a.owner_id,
        from_node_id=a.id,
        to_node_id=b.id,
        relation_type=RelationType.RELATED_TO,
        origin=RelationOrigin.USER_CREATED,
    )
    fields.update(overrides)
    return add(s, KnowledgeRelation(**fields))


# ----------------------------------------------------------------------------- schema
def test_phase_4b_tables_exist(db_session: Session):
    names = set(inspect(db_session.get_bind()).get_table_names())
    assert {
        "memories", "memory_history", "memory_evidence", "conversations", "messages",
        "message_citations", "message_memories", "knowledge_nodes", "knowledge_relations",
        "relation_evidence",
    } <= names


# ---------------------------------------------------------------------------- memories
def test_memory_defaults(db_session: Session):
    memory = make_memory(db_session, make_user(db_session))
    db_session.refresh(memory)
    assert memory.status == MemoryStatus.ACTIVE
    assert memory.verification == MemoryVerification.USER_STATED
    assert memory.created_by == MemoryActor.USER


def test_memory_confidence_must_be_between_0_and_1(db_session: Session):
    with pytest.raises(IntegrityError):
        make_memory(db_session, make_user(db_session), confidence=1.5)


def test_memory_statement_cannot_be_blank(db_session: Session):
    with pytest.raises(IntegrityError):
        make_memory(db_session, make_user(db_session), statement="   ")


def test_memory_validity_window_must_be_ordered(db_session: Session):
    now = utcnow()
    with pytest.raises(IntegrityError):
        make_memory(
            db_session, make_user(db_session), valid_from=now, valid_until=now - timedelta(days=1)
        )


def test_memory_cannot_supersede_itself(db_session: Session):
    user = make_user(db_session)
    memory = make_memory(db_session, user)
    memory.superseded_by_id = memory.id
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_deleting_successor_clears_superseded_by(db_session: Session):
    user = make_user(db_session)
    old = make_memory(db_session, user, statement="I prefer concise answers.")
    new = make_memory(db_session, user, statement="For technical topics I prefer detail.")
    old.status = MemoryStatus.SUPERSEDED
    old.superseded_by_id = new.id
    db_session.commit()

    db_session.delete(new)
    db_session.commit()
    db_session.refresh(old)

    assert old.superseded_by_id is None  # SET NULL, the old memory itself is untouched
    assert count(db_session, Memory) == 1


def test_history_version_unique_per_memory(db_session: Session):
    user = make_user(db_session)
    memory = make_memory(db_session, user)
    common = dict(
        memory_id=memory.id, owner_id=user.id, version=1,
        action=MemoryAction.CREATED, changed_by=MemoryActor.USER,
    )
    add(db_session, MemoryHistory(**common))
    with pytest.raises(IntegrityError):
        add(db_session, MemoryHistory(**common))


def test_history_cannot_belong_to_another_owners_memory(db_session: Session):
    alice, bob = make_user(db_session), make_user(db_session)
    memory = make_memory(db_session, alice)
    with pytest.raises(IntegrityError):
        add(
            db_session,
            MemoryHistory(
                memory_id=memory.id, owner_id=bob.id, version=1,
                action=MemoryAction.CREATED, changed_by=MemoryActor.USER,
            ),
        )


def test_evidence_needs_source_unless_user_statement(db_session: Session):
    user = make_user(db_session)
    memory = make_memory(db_session, user)
    add(  # a plain user statement needs no source
        db_session,
        MemoryEvidence(
            memory_id=memory.id, owner_id=user.id,
            evidence_type=EvidenceType.USER_STATEMENT, excerpt="I said so",
        ),
    )
    with pytest.raises(IntegrityError):
        add(
            db_session,
            MemoryEvidence(memory_id=memory.id, owner_id=user.id, evidence_type=EvidenceType.SOURCE),
        )


def test_evidence_cannot_use_another_owners_chunk(db_session: Session):
    alice, bob = make_user(db_session), make_user(db_session)
    alices_source = make_source(db_session, alice)
    alices_chunk = make_chunk(db_session, alices_source)
    bobs_memory = make_memory(db_session, bob)
    with pytest.raises(IntegrityError):
        add(
            db_session,
            MemoryEvidence(
                memory_id=bobs_memory.id, owner_id=bob.id, evidence_type=EvidenceType.CHUNK,
                source_id=alices_source.id, chunk_id=alices_chunk.id,
            ),
        )


def test_deleting_memory_cascades(db_session: Session):
    user = make_user(db_session)
    memory = make_memory(db_session, user)
    add(db_session, MemoryHistory(memory_id=memory.id, owner_id=user.id, version=1,
                                  action=MemoryAction.CREATED, changed_by=MemoryActor.USER))
    add(db_session, MemoryEvidence(memory_id=memory.id, owner_id=user.id,
                                   evidence_type=EvidenceType.USER_STATEMENT, excerpt="x"))
    add(db_session, KnowledgeNode(owner_id=user.id, node_type=NodeType.MEMORY, memory_id=memory.id))
    message = make_message(db_session, make_conversation(db_session, user))
    add(db_session, MessageMemory(message_id=message.id, memory_id=memory.id, owner_id=user.id))
    db_session.commit()

    db_session.delete(memory)
    db_session.commit()

    for model in (MemoryHistory, MemoryEvidence, KnowledgeNode, MessageMemory):
        assert count(db_session, model) == 0
    assert count(db_session, Message) == 1  # the answer itself stays


# -------------------------------------------------------------------- conversations
def test_message_cannot_join_another_owners_conversation(db_session: Session):
    alice, bob = make_user(db_session), make_user(db_session)
    alices_conversation = make_conversation(db_session, alice)
    with pytest.raises(IntegrityError):
        make_message(db_session, alices_conversation, owner_id=bob.id)


def test_citation_index_unique_per_message(db_session: Session):
    user = make_user(db_session)
    source = make_source(db_session, user)
    message = make_message(db_session, make_conversation(db_session, user))
    fields = dict(message_id=message.id, owner_id=user.id, citation_index=1,
                  source_id=source.id, source_title="t", excerpt="e")
    add(db_session, MessageCitation(**fields))
    with pytest.raises(IntegrityError):
        add(db_session, MessageCitation(**fields))


def test_citation_cannot_cite_another_owners_source(db_session: Session):
    alice, bob = make_user(db_session), make_user(db_session)
    alices_source = make_source(db_session, alice)
    bobs_message = make_message(db_session, make_conversation(db_session, bob))
    with pytest.raises(IntegrityError):
        add(
            db_session,
            MessageCitation(message_id=bobs_message.id, owner_id=bob.id, citation_index=1,
                            source_id=alices_source.id, source_title="t", excerpt="e"),
        )


def test_deleting_conversation_cascades(db_session: Session):
    user = make_user(db_session)
    source = make_source(db_session, user)
    memory = make_memory(db_session, user)
    conversation = make_conversation(db_session, user)
    message = make_message(db_session, conversation)
    add(db_session, MessageCitation(message_id=message.id, owner_id=user.id, citation_index=1,
                                    source_id=source.id, source_title="t", excerpt="e"))
    add(db_session, MessageMemory(message_id=message.id, memory_id=memory.id, owner_id=user.id))
    db_session.commit()

    db_session.delete(conversation)
    db_session.commit()

    for model in (Message, MessageCitation, MessageMemory):
        assert count(db_session, model) == 0
    assert count(db_session, Memory) == 1
    assert count(db_session, Source) == 1


# ---------------------------------------------------------------------------- graph
def test_node_type_must_match_its_reference(db_session: Session):
    user = make_user(db_session)
    with pytest.raises(IntegrityError):  # a concept node must have a label
        add(db_session, KnowledgeNode(owner_id=user.id, node_type=NodeType.CONCEPT))


def test_relation_cannot_be_a_self_loop(db_session: Session):
    user = make_user(db_session)
    node = make_concept(db_session, user, "rag")
    with pytest.raises(IntegrityError):
        make_relation(db_session, node, node)


def test_relation_cannot_join_nodes_of_different_owners(db_session: Session):
    alice, bob = make_user(db_session), make_user(db_session)
    a, b = make_concept(db_session, alice, "rag"), make_concept(db_session, bob, "agents")
    with pytest.raises(IntegrityError):
        make_relation(db_session, a, b)


def test_similarity_alone_cannot_claim_contradiction(db_session: Session):
    user = make_user(db_session)
    a, b = make_concept(db_session, user, "rag"), make_concept(db_session, user, "agents")
    with pytest.raises(IntegrityError):
        make_relation(db_session, a, b, relation_type=RelationType.CONTRADICTS,
                      origin=RelationOrigin.SEMANTIC_SIMILARITY)


def test_similarity_can_suggest_related_to_as_candidate(db_session: Session):
    user = make_user(db_session)
    a, b = make_concept(db_session, user, "rag"), make_concept(db_session, user, "agents")
    relation = make_relation(db_session, a, b, origin=RelationOrigin.SEMANTIC_SIMILARITY, score=0.83)
    db_session.refresh(relation)
    assert relation.status == RelationStatus.CANDIDATE


def test_duplicate_relation_rejected(db_session: Session):
    user = make_user(db_session)
    a, b = make_concept(db_session, user, "rag"), make_concept(db_session, user, "agents")
    make_relation(db_session, a, b)
    with pytest.raises(IntegrityError):
        make_relation(db_session, a, b)


# ------------------------------------------------------- the source-deletion policy
def test_deleting_source_removes_everything_derived_from_it(db_session: Session):
    user = make_user(db_session)
    source = make_source(db_session, user)
    chunk = make_chunk(db_session, source)
    memory = make_memory(db_session, user)
    add(db_session, MemoryEvidence(memory_id=memory.id, owner_id=user.id,
                                   evidence_type=EvidenceType.CHUNK,
                                   source_id=source.id, chunk_id=chunk.id, excerpt="x"))
    message = make_message(db_session, make_conversation(db_session, user))
    add(db_session, MessageCitation(message_id=message.id, owner_id=user.id, citation_index=1,
                                    source_id=source.id, chunk_id=chunk.id,
                                    source_title="t", excerpt="x"))
    source_node = make_source_node(db_session, source)
    concept = make_concept(db_session, user, "rag")
    relation = make_relation(db_session, source_node, concept)
    add(db_session, RelationEvidence(relation_id=relation.id, owner_id=user.id,
                                     chunk_id=chunk.id, excerpt="x"))
    db_session.commit()

    db_session.delete(source)
    db_session.commit()

    assert count(db_session, DocumentChunk) == 0
    assert count(db_session, MemoryEvidence) == 0
    assert count(db_session, MessageCitation) == 0
    assert count(db_session, RelationEvidence) == 0
    assert count(db_session, KnowledgeRelation) == 0
    assert count(db_session, KnowledgeNode) == 1  # only the concept node survives
    # The memory and the answer remain; services decide what to do with them (Phase 8).
    assert count(db_session, Memory) == 1
    assert count(db_session, Message) == 1


def test_deleting_user_removes_all_phase_4b_data(db_session: Session):
    user = make_user(db_session)
    memory = make_memory(db_session, user)
    add(db_session, MemoryHistory(memory_id=memory.id, owner_id=user.id, version=1,
                                  action=MemoryAction.CREATED, changed_by=MemoryActor.USER))
    make_message(db_session, make_conversation(db_session, user))
    a, b = make_concept(db_session, user, "rag"), make_concept(db_session, user, "agents")
    make_relation(db_session, a, b)
    db_session.commit()

    db_session.delete(user)
    db_session.commit()

    for model in (Memory, MemoryHistory, Conversation, Message, KnowledgeNode, KnowledgeRelation):
        assert count(db_session, model) == 0