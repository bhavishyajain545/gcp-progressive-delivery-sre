package main

import (
	"context"
	"errors"
	"sort"
	"sync"
	"time"

	"cloud.google.com/go/firestore"
	"google.golang.org/api/iterator"
)

type Order struct {
	ID        string    `json:"id" firestore:"id"`
	Item      string    `json:"item" firestore:"item"`
	Qty       int       `json:"qty" firestore:"qty"`
	Status    string    `json:"status" firestore:"status"`
	CreatedAt time.Time `json:"created_at" firestore:"created_at"`
	Version   string    `json:"created_by_version" firestore:"created_by_version"`
}

var ErrNotFound = errors.New("order not found")

type Store interface {
	Create(ctx context.Context, o Order) error
	Get(ctx context.Context, id string) (Order, error)
	List(ctx context.Context, limit int) ([]Order, error)
	Ping(ctx context.Context) error
	Close()
}

// --- memory -----------------------------------------------------------------

type MemoryStore struct {
	mu     sync.RWMutex
	orders map[string]Order
}

func NewMemoryStore() *MemoryStore { return &MemoryStore{orders: map[string]Order{}} }

func (m *MemoryStore) Create(_ context.Context, o Order) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.orders[o.ID] = o
	return nil
}

func (m *MemoryStore) Get(_ context.Context, id string) (Order, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	o, ok := m.orders[id]
	if !ok {
		return Order{}, ErrNotFound
	}
	return o, nil
}

func (m *MemoryStore) List(_ context.Context, limit int) ([]Order, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	out := make([]Order, 0, len(m.orders))
	for _, o := range m.orders {
		out = append(out, o)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].CreatedAt.After(out[j].CreatedAt) })
	if len(out) > limit {
		out = out[:limit]
	}
	return out, nil
}

func (m *MemoryStore) Ping(context.Context) error { return nil }
func (m *MemoryStore) Close()                     {}

// --- Firestore (one collection per environment) -------------------------------

type FirestoreStore struct {
	client *firestore.Client
	coll   string
}

func NewFirestoreStore(ctx context.Context, project, coll string) (*FirestoreStore, error) {
	c, err := firestore.NewClient(ctx, project)
	if err != nil {
		return nil, err
	}
	return &FirestoreStore{client: c, coll: coll}, nil
}

func (f *FirestoreStore) Create(ctx context.Context, o Order) error {
	_, err := f.client.Collection(f.coll).Doc(o.ID).Set(ctx, o)
	return err
}

func (f *FirestoreStore) Get(ctx context.Context, id string) (Order, error) {
	snap, err := f.client.Collection(f.coll).Doc(id).Get(ctx)
	if err != nil {
		return Order{}, ErrNotFound
	}
	var o Order
	return o, snap.DataTo(&o)
}

func (f *FirestoreStore) List(ctx context.Context, limit int) ([]Order, error) {
	it := f.client.Collection(f.coll).OrderBy("created_at", firestore.Desc).Limit(limit).Documents(ctx)
	defer it.Stop()
	var out []Order
	for {
		snap, err := it.Next()
		if errors.Is(err, iterator.Done) {
			return out, nil
		}
		if err != nil {
			return nil, err
		}
		var o Order
		if err := snap.DataTo(&o); err != nil {
			return nil, err
		}
		out = append(out, o)
	}
}

func (f *FirestoreStore) Ping(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	_, err := f.client.Collection(f.coll).Limit(1).Documents(ctx).GetAll()
	return err
}

func (f *FirestoreStore) Close() { _ = f.client.Close() }
