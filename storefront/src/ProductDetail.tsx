import { useEffect, useState } from "react";
import { api, shopperMessage, type Product } from "./api";
import { ProductImage } from "./ProductImage";

/**
 * One product, in full.
 *
 * Fetched fresh rather than passed down from the grid, because stock changes and a
 * detail page showing a cached availability is how a shopper ends up adding
 * something that has just sold out.
 *
 * Sizes show their remaining count where the platform gives one. Kettle & Bloom does
 * not - it reports a bare in-stock boolean - so those show "available" instead. The
 * page says what the platform knows and nothing more.
 */
export function ProductDetail({
  productId,
  onAdd,
  onBack,
  busy,
}: {
  productId: string;
  onAdd: (productId: string, variantId: string | null) => void;
  onBack: () => void;
  busy: boolean;
}) {
  const [product, setProduct] = useState<Product | null>(null);
  const [size, setSize] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setProduct(null);
    setError(null);
    api
      .product(productId)
      .then((p) => {
        setProduct(p);
        const first = p.variants.find((v) => v.availability !== "OUT_OF_STOCK");
        setSize(first?.variant_id ?? null);
      })
      .catch((e) => setError(shopperMessage(e)));
  }, [productId]);

  if (error) {
    return (
      <div className="panel">
        <div className="panel-body">
          <p className="empty">{error}</p>
          <button className="add" onClick={onBack}>
            Back to the shop
          </button>
        </div>
      </div>
    );
  }

  if (!product) return <p className="empty">Loading…</p>;

  const soldOut = product.availability === "OUT_OF_STOCK";
  const needsSize = product.variants.length > 0;
  const anySize = product.variants.some((v) => v.availability !== "OUT_OF_STOCK");
  const unavailable = soldOut || (needsSize && !anySize);
  const canAdd = !unavailable && !busy && (!needsSize || size !== null);

  return (
    <div>
      <button className="backlink" onClick={onBack}>
        &larr; Back to the shop
      </button>

      <div className="detail panel">
        <div className="panel-body">
          <ProductImage
            productId={product.product_id}
            title={product.title}
            category={product.categories[0]}
            imageUrl={product.image_url}
            soldOut={unavailable}
            large
          />

          <div className="card-dept">{product.categories[0] ?? "Gear"}</div>
          <h1 className="detail-title">{product.title}</h1>
          <p className="detail-desc">{product.description}</p>

          <div className="price-strip" style={{ marginTop: 18 }}>
            <span className="price" style={{ fontSize: 22 }}>
              {product.price?.display}
            </span>
            {product.compare_at_price && (
              <span className="price-was">{product.compare_at_price.display}</span>
            )}
            <span style={{ flex: 1 }} />
            <span
              className={`stock ${unavailable ? "OUT_OF_STOCK" : product.availability}`}
            >
              {unavailable
                ? "OUT OF STOCK"
                : product.availability.replace(/_/g, " ")}
            </span>
          </div>

          {needsSize && (
            <div style={{ marginTop: 18 }}>
              <div className="gate-label" style={{ marginBottom: 6 }}>
                {product.variants[0]?.title?.match(/g |kg|week|month/)
                  ? "Option"
                  : "Size"}
              </div>
              <div className="sizes">
                {product.variants.map((v) => {
                  const out = v.availability === "OUT_OF_STOCK";
                  return (
                    <button
                      key={v.variant_id}
                      className="size size-lg"
                      disabled={out}
                      aria-pressed={size === v.variant_id}
                      onClick={() => setSize(v.variant_id)}
                    >
                      {v.title}
                      <span className="size-left">
                        {out
                          ? "gone"
                          : v.quantity_available !== null
                            ? `${v.quantity_available} left`
                            : "available"}
                      </span>
                    </button>
                  );
                })}
              </div>
              {!anySize && (
                <p className="note" style={{ color: "var(--friction)" }}>
                  Every option is sold out.
                </p>
              )}
            </div>
          )}

          <button
            className="add"
            style={{ marginTop: 20, width: "100%", padding: 14 }}
            disabled={!canAdd}
            onClick={() => onAdd(product.product_id, size)}
          >
            {unavailable ? "Sold out" : "Add to cart"}
          </button>

          <p className="note">
            Stock is read live from the shop every time this page opens.
          </p>
        </div>
      </div>
    </div>
  );
}