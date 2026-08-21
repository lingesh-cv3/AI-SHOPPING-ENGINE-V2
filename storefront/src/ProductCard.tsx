import { useState } from "react";
import type { Product } from "./api";
import { ProductImage } from "./ProductImage";

/**
 * One product in the grid.
 *
 * Every field here arrived normalized. This component has no idea the platform
 * beneath it stores money as integer paise or reports stock as a bare boolean - it
 * reads price.display and an availability enum.
 *
 * Unavailable sizes are shown struck through rather than hidden. A shopper needs to
 * know their size exists and is gone, which is a different fact from it never
 * having existed - and it is the friction the engine recovers from.
 */
export function ProductCard({
  product,
  onAdd,
  onOpen,
  busy,
}: {
  product: Product;
  onAdd: (productId: string, variantId: string | null) => void;
  onOpen: (productId: string) => void;
  busy: boolean;
}) {
  const buyableSizes = product.variants.filter(
    (v) => v.availability !== "OUT_OF_STOCK",
  );
  const [size, setSize] = useState<string | null>(
    buyableSizes[0]?.variant_id ?? null,
  );

  const soldOut = product.availability === "OUT_OF_STOCK";
  const needsSize = product.variants.length > 0;
  const anySize = buyableSizes.length > 0;
  const unavailable = soldOut || (needsSize && !anySize);
  const canAdd = !unavailable && !busy && (!needsSize || size !== null);

  return (
    <article className="card">
      <ProductImage
        productId={product.product_id}
        title={product.title}
        category={product.categories[0]}
        imageUrl={product.image_url}
        soldOut={unavailable}
      />

      <div className="card-dept">{product.categories[0] ?? "Gear"}</div>

      <h3 className="card-title">
        <button className="titlelink" onClick={() => onOpen(product.product_id)}>
          {product.title}
        </button>
      </h3>

      <p className="card-desc">{product.description}</p>

      {needsSize && (
        <div>
          <div className="gate-label" style={{ marginBottom: 5 }}>
            Size
          </div>
          <div className="sizes">
            {product.variants.map((v) => {
              const out = v.availability === "OUT_OF_STOCK";
              return (
                <button
                  key={v.variant_id}
                  className="size"
                  disabled={out}
                  aria-pressed={size === v.variant_id}
                  onClick={() => setSize(v.variant_id)}
                  title={
                    out
                      ? "Sold out"
                      : v.quantity_available !== null
                        ? `${v.quantity_available} left`
                        : "Available"
                  }
                >
                  {v.title}
                </button>
              );
            })}
          </div>
        </div>
      )}

      <div className="price-strip">
        <span className="price">{product.price?.display}</span>
        {product.compare_at_price && (
          <span className="price-was">{product.compare_at_price.display}</span>
        )}
        <span style={{ flex: 1 }} />
        {/* Follows the variants when there are any. A product marked in stock whose
            every size is gone is not in stock, whatever the parent record says. */}
        <span className={`stock ${unavailable ? "OUT_OF_STOCK" : product.availability}`}>
          {unavailable
            ? "OUT OF STOCK"
            : product.availability.replace(/_/g, " ")}
        </span>
      </div>

      <button
        className="add"
        disabled={!canAdd}
        onClick={() => onAdd(product.product_id, size)}
      >
        {unavailable ? "Sold out" : "Add to cart"}
      </button>
    </article>
  );
}